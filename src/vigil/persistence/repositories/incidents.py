"""The incident repository (05 §5).

Three properties this file is responsible for, each of which is a bug if it fails:

  1. **An incident is written in one transaction with its transitions.** A half-written incident
     must never be visible, and an incident without its opening transition has no audit trail.
  2. **One situation is one incident.** `dedup_key` is `(camera, event_type, subject)` — the
     identity of a situation — and `open_for` is the lookup that turns a second verified event
     into an *extension* of the open incident rather than a new row. Duplicate delivery of the
     same event is idempotent: `insert` is `INSERT ... ON CONFLICT DO NOTHING` on the primary
     key, so a retried write cannot double up.
  3. **Status changes go through `transition`,** which writes the row and the audit record
     together and refuses a change that the lifecycle does not allow (06 §9). The status column
     is a cache of the last transition, never an independent truth.

Reads use the shared read-only connection and keyset pagination (08 §10): an incident list is
written to while an operator pages through it, and `OFFSET` silently skips or repeats rows when
that happens.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from vigil.domain.enums import IncidentStatus
from vigil.domain.incident import Incident, IncidentTransition
from vigil.persistence.database import Database
from vigil.persistence.mappers import (
    incident_from_row,
    incident_to_row,
    to_utc_text,
    transition_from_row,
    transition_to_row,
)
from vigil.persistence.queries import (
    IncidentFilter,
    build_where,
    keyset_clause,
    order_by,
)

INCIDENT_COLUMNS = (
    "incident_id",
    "display_ref",
    "camera_id",
    "event_type",
    "module_id",
    "module_version",
    "status",
    "severity",
    "severity_score",
    "confidence_ratio",
    "first_observed_utc",
    "confirmed_utc",
    "last_observed_utc",
    "resolved_utc",
    "duration_ms",
    "zone_ids_json",
    "location",
    "involved_track_ids_json",
    "tracker_epoch",
    "explanation_json",
    "model_metadata_json",
    "system_metadata_json",
    "operator_note",
    "dedup_key",
    "candidate_id",
)


@dataclass(frozen=True, slots=True)
class OpenIncident:
    """Just enough of an open incident to decide whether a new event extends it."""

    incident_id: str
    display_ref: str
    status: IncidentStatus
    confirmed_wall_utc: datetime
    last_observed_wall_utc: datetime
    severity_score: float


@dataclass(frozen=True, slots=True)
class IncidentLifecycle:
    """The few columns a status change needs.

    A lifecycle change must not pay to parse an incident's explanation JSON: that blob is read
    once, when an operator opens the incident, and the response path changes statuses far more
    often than anyone reads one.
    """

    incident_id: str
    display_ref: str
    status: IncidentStatus
    severity: int
    severity_score: float
    confidence_ratio: float
    confirmed_wall_utc: datetime
    last_observed_wall_utc: datetime
    duration_ms: float | None


@dataclass(frozen=True, slots=True)
class IncidentPage:
    incidents: tuple[Incident, ...]
    next_cursor: tuple[str, str] | None
    total: int


class IncidentRepository:
    __slots__ = ("_db",)

    def __init__(self, database: Database) -> None:
        self._db = database

    # ---------------------------------------------------------------- writes

    def insert(
        self,
        incident: Incident,
        *,
        dedup_key: str,
        candidate_id: str,
        evidence_status: str,
        now_wall_utc: datetime,
    ) -> bool:
        """Create an incident with its transitions. False if that id already existed.

        Idempotent by primary key, because a response worker that retries after a transient
        failure must not create a second incident for the same decision.
        """
        row = incident_to_row(incident, dedup_key=dedup_key, candidate_id=candidate_id)
        row["evidence_status"] = evidence_status
        row["updated_utc"] = to_utc_text(now_wall_utc)
        row["acknowledged_utc"] = None
        row["dismissed_utc"] = None
        columns = list(row)
        placeholders = ", ".join("?" for _ in columns)
        with self._db.write(operation="insert_incident") as connection:
            cursor = connection.execute(
                f"INSERT INTO incidents ({', '.join(columns)}) VALUES ({placeholders}) "
                "ON CONFLICT (incident_id) DO NOTHING",
                [row[c] for c in columns],
            )
            created = cursor.rowcount == 1
            cursor.close()
            if created:
                self._write_transitions(connection, incident.incident_id, incident.transitions)
        return created

    def extend(
        self,
        incident_id: str,
        *,
        last_observed_wall_utc: datetime,
        duration_ms: float,
        confidence_ratio: float,
        severity_score: float,
        severity: int,
        now_wall_utc: datetime,
    ) -> bool:
        """Record that an open situation is still happening. Never creates a row.

        `last_observed` only ever moves forward: a duplicate or out-of-order delivery of an
        older event must not rewind an incident's span. Severity and confidence take the
        *highest* value seen, because an incident is as serious as its worst moment.
        """
        with self._db.write(operation="extend_incident") as connection:
            cursor = connection.execute(
                """
                UPDATE incidents
                   SET last_observed_utc = MAX(last_observed_utc, ?),
                       duration_ms       = MAX(COALESCE(duration_ms, 0), ?),
                       confidence_ratio  = MAX(confidence_ratio, ?),
                       severity_score    = MAX(severity_score, ?),
                       severity          = MAX(severity, ?),
                       updated_utc       = ?
                 WHERE incident_id = ?
                """,
                (
                    to_utc_text(last_observed_wall_utc),
                    duration_ms,
                    confidence_ratio,
                    severity_score,
                    severity,
                    to_utc_text(now_wall_utc),
                    incident_id,
                ),
            )
            updated = cursor.rowcount == 1
            cursor.close()
        return updated

    def transition(
        self,
        incident_id: str,
        transition: IncidentTransition,
        *,
        resolved_wall_utc: datetime | None = None,
        duration_ms: float | None = None,
    ) -> bool:
        """Apply one lifecycle change and its audit row together."""
        with self._db.write(operation="transition_incident") as connection:
            cursor = connection.execute(
                """
                UPDATE incidents
                   SET status       = ?,
                       resolved_utc = COALESCE(?, resolved_utc),
                       duration_ms  = COALESCE(?, duration_ms),
                       acknowledged_utc = CASE WHEN ? = 'active' AND acknowledged_utc IS NULL
                                               THEN ? ELSE acknowledged_utc END,
                       dismissed_utc = CASE WHEN ? = 'dismissed' THEN ? ELSE dismissed_utc END,
                       updated_utc  = ?
                 WHERE incident_id = ?
                """,
                (
                    transition.to_status.value,
                    None if resolved_wall_utc is None else to_utc_text(resolved_wall_utc),
                    duration_ms,
                    transition.to_status.value,
                    to_utc_text(transition.at_wall_utc),
                    transition.to_status.value,
                    to_utc_text(transition.at_wall_utc),
                    to_utc_text(transition.at_wall_utc),
                    incident_id,
                ),
            )
            updated = cursor.rowcount == 1
            cursor.close()
            if updated:
                self._write_transitions(connection, incident_id, (transition,))
        return updated

    def set_operator_note(
        self, incident_id: str, note: str | None, *, now_wall_utc: datetime
    ) -> bool:
        with self._db.write(operation="annotate_incident") as connection:
            cursor = connection.execute(
                "UPDATE incidents SET operator_note = ?, updated_utc = ? WHERE incident_id = ?",
                (note, to_utc_text(now_wall_utc), incident_id),
            )
            updated = cursor.rowcount == 1
            cursor.close()
        return updated

    def set_evidence_status(self, incident_id: str, status: str) -> bool:
        with self._db.write(operation="evidence_status") as connection:
            cursor = connection.execute(
                "UPDATE incidents SET evidence_status = ? WHERE incident_id = ?",
                (status, incident_id),
            )
            updated = cursor.rowcount == 1
            cursor.close()
        return updated

    @staticmethod
    def _write_transitions(
        connection: sqlite3.Connection,
        incident_id: str,
        transitions: Sequence[IncidentTransition],
    ) -> None:
        for transition in transitions:
            row = transition_to_row(incident_id, transition)
            connection.execute(
                "INSERT INTO incident_transitions "
                "(incident_id, from_status, to_status, at_utc, actor, reason) "
                "VALUES (:incident_id, :from_status, :to_status, :at_utc, :actor, :reason)",
                row,
            )

    def delete_older_than(self, cutoff_wall_utc: datetime, *, limit: int) -> tuple[str, ...]:
        """Delete retention-eligible incidents, returning their ids. Never touches an incident
        that is still open, annotated, or still has evidence rows (05 §8)."""
        rows = self._db.query(
            """
            SELECT incident_id FROM incidents
             WHERE confirmed_utc < ?
               AND status NOT IN ('active', 'confirmed', 'candidate', 'resolving')
               AND (operator_note IS NULL OR operator_note = '')
               AND NOT EXISTS (SELECT 1 FROM evidence e WHERE e.incident_id = incidents.incident_id)
             ORDER BY confirmed_utc ASC
             LIMIT ?
            """,
            (to_utc_text(cutoff_wall_utc), limit),
        )
        ids = tuple(str(r["incident_id"]) for r in rows)
        if not ids:
            return ()
        with self._db.write(operation="delete_incidents") as connection:
            connection.executemany(
                "DELETE FROM incidents WHERE incident_id = ?", [(i,) for i in ids]
            )
        return ids

    # ---------------------------------------------------------------- reads

    def open_for(self, dedup_key: str) -> OpenIncident | None:
        """The open incident for this situation, if there is one. The dedup lookup."""
        row = self._db.query_one(
            """
            SELECT incident_id, display_ref, status, confirmed_utc, last_observed_utc,
                   severity_score
              FROM incidents
             WHERE dedup_key = ?
               AND status IN ('candidate', 'confirmed', 'active', 'resolving')
             ORDER BY confirmed_utc DESC
             LIMIT 1
            """,
            (dedup_key,),
        )
        if row is None:
            return None
        from vigil.persistence.mappers import from_utc_text

        return OpenIncident(
            incident_id=str(row["incident_id"]),
            display_ref=str(row["display_ref"]),
            status=IncidentStatus(row["status"]),
            confirmed_wall_utc=from_utc_text(row["confirmed_utc"]),
            last_observed_wall_utc=from_utc_text(row["last_observed_utc"]),
            severity_score=float(row["severity_score"]),
        )

    def lifecycle(self, incident_id: str) -> IncidentLifecycle | None:
        row = self._db.query_one(
            """
            SELECT incident_id, display_ref, status, severity, severity_score, confidence_ratio,
                   confirmed_utc, last_observed_utc, duration_ms
              FROM incidents WHERE incident_id = ?
            """,
            (incident_id,),
        )
        if row is None:
            return None
        from vigil.persistence.mappers import from_utc_text

        return IncidentLifecycle(
            incident_id=str(row["incident_id"]),
            display_ref=str(row["display_ref"]),
            status=IncidentStatus(row["status"]),
            severity=int(row["severity"]),
            severity_score=float(row["severity_score"]),
            confidence_ratio=float(row["confidence_ratio"]),
            confirmed_wall_utc=from_utc_text(row["confirmed_utc"]),
            last_observed_wall_utc=from_utc_text(row["last_observed_utc"]),
            duration_ms=None if row["duration_ms"] is None else float(row["duration_ms"]),
        )

    def get(self, incident_id: str) -> Incident | None:
        row = self._db.query_one(
            f"SELECT {', '.join(INCIDENT_COLUMNS)} FROM incidents WHERE incident_id = ?",
            (incident_id,),
        )
        if row is None:
            return None
        return incident_from_row(row, self.transitions(incident_id))

    def by_display_ref(self, display_ref: str) -> Incident | None:
        row = self._db.query_one(
            f"SELECT {', '.join(INCIDENT_COLUMNS)} FROM incidents WHERE display_ref = ?",
            (display_ref,),
        )
        if row is None:
            return None
        return incident_from_row(row, self.transitions(str(row["incident_id"])))

    def transitions(self, incident_id: str) -> tuple[IncidentTransition, ...]:
        rows = self._db.query(
            "SELECT from_status, to_status, at_utc, actor, reason FROM incident_transitions "
            "WHERE incident_id = ? ORDER BY id ASC",
            (incident_id,),
        )
        return tuple(transition_from_row(r) for r in rows)

    def transitions_for(
        self, incident_ids: Sequence[str]
    ) -> dict[str, tuple[IncidentTransition, ...]]:
        """Transitions for a whole page in one query.

        `page()` deliberately returns incidents without their transitions, because a list of
        twelve rows should not pay for their audit trails. A report *does* need them -- the
        timeline is part of what an export is for -- so it asks for them in one statement
        rather than one per row.
        """
        if not incident_ids:
            return {}
        placeholders = ", ".join("?" for _ in incident_ids)
        rows = self._db.query(
            "SELECT incident_id, from_status, to_status, at_utc, actor, reason "
            f"FROM incident_transitions WHERE incident_id IN ({placeholders}) "
            "ORDER BY incident_id, id ASC",
            list(incident_ids),
        )
        out: dict[str, list[IncidentTransition]] = {}
        for row in rows:
            out.setdefault(str(row["incident_id"]), []).append(transition_from_row(row))
        return {incident_id: tuple(items) for incident_id, items in out.items()}

    def evidence_status(self, incident_id: str) -> str | None:
        value = self._db.scalar(
            "SELECT evidence_status FROM incidents WHERE incident_id = ?", (incident_id,)
        )
        return None if value is None else str(value)

    def count(self, filters: IncidentFilter) -> int:
        where = build_where(filters, alias="incidents")
        value = self._db.scalar(f"SELECT COUNT(*) FROM incidents{where.rendered}", where.params)
        return int(value or 0)

    def page(
        self,
        filters: IncidentFilter,
        *,
        limit: int,
        cursor: tuple[str, str] | None = None,
        sort: str = "confirmed",
        descending: bool = True,
    ) -> IncidentPage:
        where = build_where(filters, alias="incidents")
        keyset, keyset_params = keyset_clause(
            cursor, sort, descending=descending, alias="incidents"
        )
        if keyset:
            where.and_(keyset, *keyset_params)
        sql = (
            f"SELECT {', '.join(f'incidents.{c}' for c in INCIDENT_COLUMNS)} FROM incidents"
            f"{where.rendered} {order_by(sort, descending=descending, alias='incidents')} "
            "LIMIT ?"
        )
        rows = self._db.query(sql, [*where.params, limit + 1])
        more = len(rows) > limit
        rows = rows[:limit]
        incidents = tuple(incident_from_row(r) for r in rows)
        next_cursor: tuple[str, str] | None = None
        if more and rows:
            from vigil.persistence.queries import SORT_COLUMNS

            column = SORT_COLUMNS[sort]
            last = rows[-1]
            next_cursor = (str(last[column]), str(last["incident_id"]))
        return IncidentPage(incidents=incidents, next_cursor=next_cursor, total=self.count(filters))
