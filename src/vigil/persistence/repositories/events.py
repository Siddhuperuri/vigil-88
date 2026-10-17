"""Candidate events and camera-health events (05 §5).

**Rejected candidates are persisted.** Without them you only ever see what got through, and you
are tuning suppression blind (06 §6). They are also the fastest way to discover that a validator
is silently eating real events, which is the failure mode nobody notices. Retained under
`retention.candidate_days`, which is shorter than incidents because the volume is much higher.

Camera-health events live here too, because the single most useful correlation in the product is
health and incidents on one axis: *"the camera was reconnecting when that fired"* answers a large
fraction of "why did this fire" questions (08 §3).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from vigil.domain.enums import CameraState, CandidateOutcome, EventType
from vigil.domain.event import CandidateRecord
from vigil.persistence.database import Database
from vigil.persistence.mappers import candidate_to_row, from_utc_text, to_utc_text

CANDIDATE_COLUMNS = (
    "candidate_id",
    "camera_id",
    "module_id",
    "event_type",
    "subject",
    "log_odds",
    "confidence_ratio",
    "support_ratio",
    "observations",
    "promoted_utc",
    "outcome",
    "rejection_reason",
    "verdicts_json",
    "trace_json",
    "incident_id",
)


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """The rejection feed's shape. Deliberately not a domain object: the feed shows a summary,
    and loading every observation of every rejection to render a list would be waste."""

    candidate_id: str
    camera_id: str
    module_id: str
    event_type: EventType
    subject: str
    outcome: CandidateOutcome
    rejection_reason: str | None
    confidence_ratio: float
    log_odds: float
    support_ratio: float
    observations: int
    promoted_wall_utc: datetime
    incident_id: str | None


@dataclass(frozen=True, slots=True)
class HealthEvent:
    camera_id: str
    state: CameraState
    at_wall_utc: datetime
    detail: str | None


def _candidate_row(data: sqlite3.Row) -> CandidateRow:
    return CandidateRow(
        candidate_id=str(data["candidate_id"]),
        camera_id=str(data["camera_id"]),
        module_id=str(data["module_id"]),
        event_type=EventType(data["event_type"]),
        subject=str(data["subject"]),
        outcome=CandidateOutcome(data["outcome"]),
        rejection_reason=data["rejection_reason"],
        confidence_ratio=float(data["confidence_ratio"]),
        log_odds=float(data["log_odds"]),
        support_ratio=float(data["support_ratio"]),
        observations=int(data["observations"]),
        promoted_wall_utc=from_utc_text(data["promoted_utc"]),
        incident_id=data["incident_id"],
    )


class CandidateRepository:
    __slots__ = ("_db",)

    def __init__(self, database: Database) -> None:
        self._db = database

    def record_all(
        self, records: Sequence[CandidateRecord], *, incident_ids: dict[str, str] | None = None
    ) -> int:
        """Persist every candidate outcome in one transaction.

        `ON CONFLICT ... DO UPDATE` because a candidate held in DEFER is recorded as PENDING
        first and decided later: the row is the candidate's current standing, not an append-only
        log, and its id is stable.
        """
        if not records:
            return 0
        mapping = incident_ids or {}
        rows = [
            candidate_to_row(r, incident_id=mapping.get(r.candidate.candidate_id)) for r in records
        ]
        placeholders = ", ".join(f":{c}" for c in CANDIDATE_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in CANDIDATE_COLUMNS if c != "candidate_id")
        with self._db.write(operation="insert_candidates") as connection:
            connection.executemany(
                f"INSERT INTO candidate_events ({', '.join(CANDIDATE_COLUMNS)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT (candidate_id) DO UPDATE SET {updates}",
                rows,
            )
        return len(rows)

    def recent(
        self,
        *,
        limit: int,
        camera_id: str | None = None,
        outcomes: Sequence[CandidateOutcome] = (),
        since_wall_utc: datetime | None = None,
    ) -> tuple[CandidateRow, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if camera_id is not None:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        if outcomes:
            clauses.append(f"outcome IN ({', '.join('?' for _ in outcomes)})")
            params.extend(o.value for o in outcomes)
        if since_wall_utc is not None:
            clauses.append("promoted_utc >= ?")
            params.append(to_utc_text(since_wall_utc))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.query(
            f"SELECT {', '.join(CANDIDATE_COLUMNS)} FROM candidate_events{where} "
            "ORDER BY promoted_utc DESC, candidate_id DESC LIMIT ?",
            [*params, limit],
        )
        return tuple(_candidate_row(r) for r in rows)

    def get(self, candidate_id: str) -> CandidateRow | None:
        row = self._db.query_one(
            f"SELECT {', '.join(CANDIDATE_COLUMNS)} FROM candidate_events WHERE candidate_id = ?",
            (candidate_id,),
        )
        return None if row is None else _candidate_row(row)

    def verdicts_json(self, candidate_id: str) -> str | None:
        value = self._db.scalar(
            "SELECT verdicts_json FROM candidate_events WHERE candidate_id = ?", (candidate_id,)
        )
        return None if value is None else str(value)

    def counts_by_outcome(
        self, *, since_wall_utc: datetime | None = None, camera_id: str | None = None
    ) -> dict[CandidateOutcome, int]:
        clauses: list[str] = []
        params: list[object] = []
        if since_wall_utc is not None:
            clauses.append("promoted_utc >= ?")
            params.append(to_utc_text(since_wall_utc))
        if camera_id is not None:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.query(
            f"SELECT outcome, COUNT(*) AS n FROM candidate_events{where} GROUP BY outcome",
            params,
        )
        return {CandidateOutcome(r["outcome"]): int(r["n"]) for r in rows}

    def rejection_reasons(
        self, *, limit: int, since_wall_utc: datetime | None = None
    ) -> tuple[tuple[str, int], ...]:
        """Which validators are rejecting the most, newest window first. Tuning input."""
        params: list[object] = []
        where = "WHERE outcome = 'rejected' AND rejection_reason IS NOT NULL"
        if since_wall_utc is not None:
            where += " AND promoted_utc >= ?"
            params.append(to_utc_text(since_wall_utc))
        rows = self._db.query(
            f"SELECT rejection_reason, COUNT(*) AS n FROM candidate_events {where} "
            "GROUP BY rejection_reason ORDER BY n DESC LIMIT ?",
            [*params, limit],
        )
        return tuple((str(r["rejection_reason"]), int(r["n"])) for r in rows)

    def delete_older_than(self, cutoff_wall_utc: datetime, *, limit: int) -> int:
        with self._db.write(operation="delete_candidates") as connection:
            cursor = connection.execute(
                "DELETE FROM candidate_events WHERE candidate_id IN ("
                "  SELECT candidate_id FROM candidate_events WHERE promoted_utc < ? "
                "  ORDER BY promoted_utc ASC LIMIT ?)",
                (to_utc_text(cutoff_wall_utc), limit),
            )
            removed = cursor.rowcount
            cursor.close()
        return max(removed, 0)


class HealthEventRepository:
    __slots__ = ("_db",)

    def __init__(self, database: Database) -> None:
        self._db = database

    def record(
        self, camera_id: str, state: CameraState, at_wall_utc: datetime, detail: str | None
    ) -> None:
        with self._db.write(operation="insert_health_event") as connection:
            connection.execute(
                "INSERT INTO camera_health_events (camera_id, state, at_utc, detail) "
                "VALUES (?, ?, ?, ?)",
                (camera_id, state.value, to_utc_text(at_wall_utc), detail),
            )

    def recent(
        self, *, limit: int, camera_id: str | None = None, since_wall_utc: datetime | None = None
    ) -> tuple[HealthEvent, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if camera_id is not None:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        if since_wall_utc is not None:
            clauses.append("at_utc >= ?")
            params.append(to_utc_text(since_wall_utc))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.query(
            f"SELECT camera_id, state, at_utc, detail FROM camera_health_events{where} "
            "ORDER BY at_utc DESC, id DESC LIMIT ?",
            [*params, limit],
        )
        return tuple(
            HealthEvent(
                camera_id=str(r["camera_id"]),
                state=CameraState(r["state"]),
                at_wall_utc=from_utc_text(r["at_utc"]),
                detail=r["detail"],
            )
            for r in rows
        )

    def duration_by_state(
        self, camera_id: str, *, since_wall_utc: datetime, until_wall_utc: datetime
    ) -> dict[CameraState, float]:
        """Seconds spent in each state inside the window, from the recorded transitions.

        Computed from consecutive events rather than sampled, so it reflects what actually
        happened. The window's leading edge uses the last state known before it.
        """
        rows = self._db.query(
            "SELECT state, at_utc FROM camera_health_events WHERE camera_id = ? AND at_utc < ? "
            "ORDER BY at_utc ASC",
            (camera_id, to_utc_text(until_wall_utc)),
        )
        if not rows:
            return {}
        totals: dict[CameraState, float] = {}
        previous_state: CameraState | None = None
        previous_at: datetime | None = None
        for row in rows:
            at = from_utc_text(row["at_utc"])
            if previous_state is not None and previous_at is not None:
                start = max(previous_at, since_wall_utc)
                if at > start:
                    totals[previous_state] = (
                        totals.get(previous_state, 0.0) + (at - start).total_seconds()
                    )
            previous_state, previous_at = CameraState(row["state"]), at
        if previous_state is not None and previous_at is not None:
            start = max(previous_at, since_wall_utc)
            if until_wall_utc > start:
                totals[previous_state] = (
                    totals.get(previous_state, 0.0) + (until_wall_utc - start).total_seconds()
                )
        return totals

    def delete_older_than(self, cutoff_wall_utc: datetime, *, limit: int) -> int:
        with self._db.write(operation="delete_health_events") as connection:
            cursor = connection.execute(
                "DELETE FROM camera_health_events WHERE id IN ("
                "  SELECT id FROM camera_health_events WHERE at_utc < ? ORDER BY at_utc ASC "
                "  LIMIT ?)",
                (to_utc_text(cutoff_wall_utc), limit),
            )
            removed = cursor.rowcount
            cursor.close()
        return max(removed, 0)
