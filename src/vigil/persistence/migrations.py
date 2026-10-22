"""Schema migrations (05 §5).

A monitoring system accumulates data you cannot throw away on every schema change, so the
schema is versioned and migrated forward. Four rules, each a failure this avoids:

  1. **Forward only, one transaction each.** A migration that fails leaves the database at the
     previous version, not half-way.
  2. **Never destructive.** No migration drops a table or a column that holds incident history.
     Adding a column uses `ALTER TABLE ... ADD COLUMN` with a default, so existing rows stay
     readable — which is what "backward compatibility with existing records" means in practice.
  3. **A newer database is an error, not an invitation.** If the file was written by a later
     version of the code, this refuses to open it rather than guessing at columns it does not
     know. Downgrading by deleting the database is the operator's decision, never ours.
  4. **Recorded.** `schema_version` keeps every applied version with its name and timestamp, so
     "which schema produced this row" is answerable.

Each migration is plain SQL. Indexes are declared with the migration that needs them, and every
one of them is justified by a query the console or the analytics layer actually makes (05 §5).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.core.protocols.logger import LoggerLike

SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_utc TEXT   NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MigrationReport:
    version: int
    applied: tuple[str, ...]
    already_current: bool


_V1 = (
    """
    CREATE TABLE cameras (
        camera_id       TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        source_kind     TEXT NOT NULL,
        source_locator  TEXT,
        location        TEXT,
        priority        INTEGER NOT NULL,
        enabled         INTEGER NOT NULL,
        config_json     TEXT NOT NULL,
        created_utc     TEXT NOT NULL,
        updated_utc     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE incidents (
        incident_id             TEXT PRIMARY KEY,
        display_ref             TEXT NOT NULL UNIQUE,
        camera_id               TEXT NOT NULL,
        event_type              TEXT NOT NULL,
        module_id               TEXT NOT NULL,
        module_version          TEXT NOT NULL,
        status                  TEXT NOT NULL,
        severity                INTEGER NOT NULL,
        severity_score          REAL NOT NULL,
        confidence_ratio        REAL NOT NULL,
        first_observed_utc      TEXT NOT NULL,
        confirmed_utc           TEXT NOT NULL,
        last_observed_utc       TEXT NOT NULL,
        resolved_utc            TEXT,
        duration_ms             REAL,
        zone_ids_json           TEXT NOT NULL,
        location                TEXT,
        involved_track_ids_json TEXT NOT NULL,
        tracker_epoch           INTEGER NOT NULL,
        explanation_json        TEXT NOT NULL,
        model_metadata_json     TEXT NOT NULL,
        system_metadata_json    TEXT NOT NULL,
        operator_note           TEXT,
        dedup_key               TEXT NOT NULL,
        candidate_id            TEXT NOT NULL,
        evidence_status         TEXT NOT NULL,
        acknowledged_utc        TEXT,
        dismissed_utc           TEXT,
        updated_utc             TEXT NOT NULL
    )
    """,
    # The incident list and timeline read newest-first; the open queue sorts by severity;
    # per-camera and per-type are the analytics breakdowns (05 §5).
    "CREATE INDEX idx_incidents_confirmed ON incidents (confirmed_utc DESC)",
    "CREATE INDEX idx_incidents_status_severity ON incidents (status, severity DESC)",
    "CREATE INDEX idx_incidents_camera ON incidents (camera_id, confirmed_utc DESC)",
    "CREATE INDEX idx_incidents_type ON incidents (event_type, confirmed_utc DESC)",
    # The dedup lookup: "is there an open incident for this situation". Without it every
    # verified event would scan the whole table, which is the one query on the response path.
    "CREATE INDEX idx_incidents_dedup ON incidents (dedup_key, status)",
    """
    CREATE TABLE incident_transitions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT NOT NULL REFERENCES incidents (incident_id) ON DELETE CASCADE,
        from_status TEXT,
        to_status   TEXT NOT NULL,
        at_utc      TEXT NOT NULL,
        actor       TEXT NOT NULL,
        reason      TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_transitions_incident ON incident_transitions (incident_id, id)",
    """
    CREATE TABLE evidence (
        evidence_id    TEXT PRIMARY KEY,
        incident_id    TEXT NOT NULL REFERENCES incidents (incident_id) ON DELETE CASCADE,
        kind           TEXT NOT NULL,
        relative_path  TEXT NOT NULL,
        sha256         TEXT NOT NULL,
        bytes          INTEGER NOT NULL,
        media_type     TEXT NOT NULL,
        captured_utc   TEXT NOT NULL,
        span_start_ms  REAL,
        span_end_ms    REAL,
        expires_utc    TEXT
    )
    """,
    "CREATE INDEX idx_evidence_incident ON evidence (incident_id)",
    "CREATE INDEX idx_evidence_expiry ON evidence (expires_utc)",
    """
    CREATE TABLE candidate_events (
        candidate_id     TEXT PRIMARY KEY,
        camera_id        TEXT NOT NULL,
        module_id        TEXT NOT NULL,
        event_type       TEXT NOT NULL,
        subject          TEXT NOT NULL,
        log_odds         REAL NOT NULL,
        confidence_ratio REAL NOT NULL,
        support_ratio    REAL NOT NULL,
        observations     INTEGER NOT NULL,
        promoted_utc     TEXT NOT NULL,
        outcome          TEXT NOT NULL,
        rejection_reason TEXT,
        verdicts_json    TEXT NOT NULL,
        trace_json       TEXT NOT NULL,
        incident_id      TEXT
    )
    """,
    "CREATE INDEX idx_candidates_promoted ON candidate_events (promoted_utc DESC)",
    "CREATE INDEX idx_candidates_outcome ON candidate_events (outcome, promoted_utc DESC)",
    "CREATE INDEX idx_candidates_camera ON candidate_events (camera_id, promoted_utc DESC)",
    """
    CREATE TABLE camera_health_events (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id TEXT NOT NULL,
        state     TEXT NOT NULL,
        at_utc    TEXT NOT NULL,
        detail    TEXT
    )
    """,
    "CREATE INDEX idx_health_camera ON camera_health_events (camera_id, at_utc DESC)",
    "CREATE INDEX idx_health_at ON camera_health_events (at_utc DESC)",
    """
    CREATE TABLE system_metrics (
        t_utc                     TEXT PRIMARY KEY,
        cpu_percent               REAL NOT NULL,
        memory_used_mb            REAL NOT NULL,
        process_rss_mb            REAL NOT NULL,
        gpu_utilization_percent   REAL,
        gpu_memory_used_mb        REAL,
        gpu_temperature_c         REAL,
        pipeline_fps              REAL,
        inference_latency_p50_ms  REAL,
        inference_latency_p95_ms  REAL,
        queue_depths_json         TEXT NOT NULL,
        frames_dropped_total      INTEGER NOT NULL
    )
    """,
    "CREATE INDEX idx_metrics_t ON system_metrics (t_utc DESC)",
    """
    CREATE TABLE mutes (
        mute_id      TEXT PRIMARY KEY,
        camera_id    TEXT,
        zone_id      TEXT,
        event_type   TEXT,
        created_utc  TEXT NOT NULL,
        expires_utc  TEXT NOT NULL,
        reason       TEXT NOT NULL,
        actor        TEXT NOT NULL,
        revoked_utc  TEXT
    )
    """,
    "CREATE INDEX idx_mutes_expiry ON mutes (expires_utc)",
)

MIGRATIONS: tuple[Migration, ...] = (Migration(1, "initial_schema", _V1),)


def apply_migrations(
    connection: sqlite3.Connection, *, logger: LoggerLike, clock: Clock
) -> MigrationReport:
    """Bring the database to the newest version this build knows. Never destructive."""
    try:
        connection.execute(SCHEMA_VERSION_DDL)
    except sqlite3.Error as exc:
        raise PersistenceError(
            "cannot create the schema_version table", context={"error": str(exc)}
        ) from exc

    current = _current_version(connection)
    newest = MIGRATIONS[-1].version if MIGRATIONS else 0
    if current > newest:
        raise PersistenceError(
            "the incident database was written by a newer version of VIGIL-88, which this "
            "build cannot read safely: upgrade VIGIL-88, or move the database file aside to "
            "start a new one (nothing is deleted automatically)",
            context={"database_version": current, "this_build_knows": newest},
        )

    applied: list[str] = []
    for migration in MIGRATIONS:
        if migration.version <= current:
            continue
        _apply_one(connection, migration, logger=logger, clock=clock)
        applied.append(f"{migration.version:03d}_{migration.name}")
    return MigrationReport(
        version=_current_version(connection),
        applied=tuple(applied),
        already_current=not applied,
    )


def _current_version(connection: sqlite3.Connection) -> int:
    cursor = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    return int(row[0] or 0)


def _apply_one(
    connection: sqlite3.Connection,
    migration: Migration,
    *,
    logger: LoggerLike,
    clock: Clock,
) -> None:
    statements: Sequence[str] = migration.statements
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (version, name, applied_utc) VALUES (?, ?, ?)",
            (migration.version, migration.name, clock.wall_utc().isoformat()),
        )
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error as rollback_error:
            logger.error("migration_rollback_failed", error=str(rollback_error))
        raise PersistenceError(
            f"migration {migration.version} ({migration.name}) failed",
            context={"error": str(exc)},
        ) from exc
    logger.info("migration_applied", version=migration.version, name=migration.name)
