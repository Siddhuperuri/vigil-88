"""The evidence repository (05 §5, §6).

Evidence rows and evidence files are two halves of one fact, and the order they are written in
is what keeps them consistent: the **file is written and hashed first**, then the row. A row
without a file would make the console offer a download that 404s; a file without a row is an
orphan the retention sweep cleans up. Of the two, the orphan is the harmless one.

`relative_path` is relative to `paths.evidence_dir` and never absolute (01 §10), so the data
directory is movable — and the `Evidence` constructor refuses a path containing `..`, so a
traversal cannot be stored even if something upstream were tricked into producing one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from vigil.domain.evidence import Evidence
from vigil.persistence.database import Database
from vigil.persistence.mappers import evidence_from_row, evidence_to_row, to_utc_text

EVIDENCE_COLUMNS = (
    "evidence_id",
    "incident_id",
    "kind",
    "relative_path",
    "sha256",
    "bytes",
    "media_type",
    "captured_utc",
    "span_start_ms",
    "span_end_ms",
    "expires_utc",
)


class EvidenceRepository:
    __slots__ = ("_db",)

    def __init__(self, database: Database) -> None:
        self._db = database

    def add_all(self, records: Sequence[Evidence]) -> int:
        """Insert every record in one transaction. Idempotent on evidence_id."""
        if not records:
            return 0
        rows = [evidence_to_row(e) for e in records]
        placeholders = ", ".join(f":{c}" for c in EVIDENCE_COLUMNS)
        with self._db.write(operation="insert_evidence") as connection:
            cursor = connection.executemany(
                f"INSERT INTO evidence ({', '.join(EVIDENCE_COLUMNS)}) VALUES ({placeholders}) "
                "ON CONFLICT (evidence_id) DO NOTHING",
                rows,
            )
            written = cursor.rowcount
            cursor.close()
        return max(written, 0)

    def for_incident(self, incident_id: str) -> tuple[Evidence, ...]:
        rows = self._db.query(
            f"SELECT {', '.join(EVIDENCE_COLUMNS)} FROM evidence WHERE incident_id = ? "
            "ORDER BY kind ASC, captured_utc ASC",
            (incident_id,),
        )
        return tuple(evidence_from_row(r) for r in rows)

    def get(self, evidence_id: str) -> Evidence | None:
        row = self._db.query_one(
            f"SELECT {', '.join(EVIDENCE_COLUMNS)} FROM evidence WHERE evidence_id = ?",
            (evidence_id,),
        )
        return None if row is None else evidence_from_row(row)

    def ids_for(self, incident_id: str) -> tuple[str, ...]:
        rows = self._db.query(
            "SELECT evidence_id FROM evidence WHERE incident_id = ? ORDER BY evidence_id",
            (incident_id,),
        )
        return tuple(str(r["evidence_id"]) for r in rows)

    def counts_by_incident(self, incident_ids: Sequence[str]) -> dict[str, int]:
        """One query for a whole page of incidents, so the list does not fan out per row."""
        if not incident_ids:
            return {}
        placeholders = ", ".join("?" for _ in incident_ids)
        rows = self._db.query(
            f"SELECT incident_id, COUNT(*) AS n FROM evidence WHERE incident_id IN "
            f"({placeholders}) GROUP BY incident_id",
            list(incident_ids),
        )
        return {str(r["incident_id"]): int(r["n"]) for r in rows}

    def expired(self, now_wall_utc: datetime, *, limit: int) -> tuple[Evidence, ...]:
        """Evidence past its own expiry, oldest first. Retention deletes files then rows."""
        rows = self._db.query(
            f"SELECT {', '.join(EVIDENCE_COLUMNS)} FROM evidence "
            "WHERE expires_utc IS NOT NULL AND expires_utc < ? "
            "ORDER BY expires_utc ASC LIMIT ?",
            (to_utc_text(now_wall_utc), limit),
        )
        return tuple(evidence_from_row(r) for r in rows)

    def delete(self, evidence_ids: Sequence[str]) -> int:
        if not evidence_ids:
            return 0
        with self._db.write(operation="delete_evidence") as connection:
            cursor = connection.executemany(
                "DELETE FROM evidence WHERE evidence_id = ?", [(i,) for i in evidence_ids]
            )
            removed = cursor.rowcount
            cursor.close()
        return max(removed, 0)

    def total_bytes(self) -> int:
        return int(self._db.scalar("SELECT COALESCE(SUM(bytes), 0) FROM evidence") or 0)
