"""The SQLite connection and its discipline (05 §5, D-006).

Stdlib `sqlite3`, not an ORM. 03 §1 rule 6 names `sqlite3` among the stdlib modules that cover
this system, and the reasons it wins here are specific rather than stylistic:

  * the write discipline of 05 §5 is the design — one writer, readers on their own
    connections, explicit PRAGMAs, one transaction per incident — and a connection pool sitting
    between this file and those guarantees makes them harder to verify, not easier;
  * analytics (P5) is aggregate SQL over indexed columns, which is what SQL is for and what an
    ORM's query builder obscures;
  * it adds no runtime dependency to a project whose first dependency rule is that every
    dependency is a liability.

The cost is a hand-written migration runner (`migrations.py`) and hand-written row mapping
(`mappers.py`), both small and both tested. Recorded as a deviation in ADR 0013; the repository
interfaces are unchanged, so D-006's "Postgres is a later swap" still holds.

**Concurrency.** WAL permits one writer and many concurrent readers, which is exactly this
access pattern. Writes are serialised by a process-level lock so SQLite's own busy handling is
never the thing that fails, and every write is one transaction: a half-written incident is never
visible. Readers get a thread-local connection, so the API's threadpool never shares a cursor.

**Failures are visible.** Every failure becomes a `PersistenceError` with the statement that
failed, counted in `vigil_db_errors_total`. Nothing here swallows an error, and nothing here
deletes or recreates a database to recover from one (05 §5: no silent resets).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from vigil.config.schema.storage import StorageConfig
from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import ns_to_ms
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.migrations import MIGRATIONS, MigrationReport, apply_migrations

Row = sqlite3.Row
Params = Sequence[object] | dict[str, object]


class Database:
    """One per process. Owns the writer connection and hands out reader connections."""

    __slots__ = (
        "_clock",
        "_config",
        "_local",
        "_log",
        "_metrics",
        "_path",
        "_readers",
        "_write_lock",
        "_writer",
    )

    def __init__(
        self,
        path: Path,
        *,
        config: StorageConfig,
        clock: Clock,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._path = path
        self._config = config
        self._clock = clock
        self._log = logger.bind(component="database")
        self._metrics = metrics
        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._writer: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    # ---------------------------------------------------------------- lifecycle

    def open(self) -> MigrationReport:
        """Open the database, apply the PRAGMAs, and migrate it forward."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect(read_only=False)
        self._writer = connection
        report = apply_migrations(connection, logger=self._log, clock=self._clock)
        self._log.info(
            "database_ready",
            path=self._path.name,
            schema_version=report.version,
            applied=list(report.applied),
        )
        return report

    def close(self) -> None:
        """Idempotent. Checkpoints the WAL so the main database file is self-contained."""
        with self._write_lock:
            if self._writer is not None:
                try:
                    self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._writer.close()
                except sqlite3.Error as exc:
                    self._metrics.counter("vigil_db_errors_total", operation="close").inc()
                    self._log.warning("database_close_failed", error=str(exc))
                self._writer = None
            for reader in self._readers:
                try:
                    reader.close()
                except sqlite3.Error as exc:
                    self._log.warning("database_reader_close_failed", error=str(exc))
            self._readers.clear()

    def __enter__(self) -> Database:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ---------------------------------------------------------------- connections

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        uri = self._path.as_uri()
        target = f"{uri}?mode=ro" if read_only else uri
        try:
            connection = sqlite3.connect(
                target,
                uri=True,
                timeout=self._config.busy_timeout_ms / 1000.0,
                isolation_level=None,  # explicit BEGIN/COMMIT: see `write()`
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            self._metrics.counter("vigil_db_errors_total", operation="connect").inc()
            raise PersistenceError(
                f"cannot open the incident database at {self._path}",
                context={"error": str(exc), "read_only": read_only},
            ) from exc
        connection.row_factory = sqlite3.Row
        self._apply_pragmas(connection, read_only=read_only)
        return connection

    def _apply_pragmas(self, connection: sqlite3.Connection, *, read_only: bool) -> None:
        statements = [
            f"PRAGMA busy_timeout = {self._config.busy_timeout_ms}",
            f"PRAGMA cache_size = -{self._config.page_cache_kb}",
            "PRAGMA foreign_keys = ON",
        ]
        if not read_only:
            statements = [
                "PRAGMA journal_mode = WAL",
                f"PRAGMA synchronous = {self._config.synchronous.upper()}",
                *statements,
            ]
        for statement in statements:
            try:
                connection.execute(statement)
            except sqlite3.Error as exc:
                self._metrics.counter("vigil_db_errors_total", operation="pragma").inc()
                raise PersistenceError(
                    "cannot configure the incident database",
                    context={"pragma": statement, "error": str(exc)},
                ) from exc

    def reader(self) -> sqlite3.Connection:
        """A read-only connection owned by the calling thread."""
        existing: sqlite3.Connection | None = getattr(self._local, "reader", None)
        if existing is not None:
            return existing
        connection = self._connect(read_only=True)
        self._local.reader = connection
        with self._write_lock:
            self._readers.append(connection)
        return connection

    # ---------------------------------------------------------------- transactions

    @contextmanager
    def write(self, *, operation: str = "write") -> Iterator[sqlite3.Connection]:
        """One serialised transaction. Rolls back and raises `PersistenceError` on failure.

        The caller gets a connection, not a cursor, so one transaction can hold an incident,
        its transitions and its evidence rows together — which is the point (05 §5).
        """
        started = self._clock.monotonic_ns()
        with self._write_lock:
            connection = self._require_writer()
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                self._fail(operation, exc)
            try:
                yield connection
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(connection)
                self._fail(operation, exc)
            except BaseException:
                self._rollback(connection)
                raise
        self._metrics.histogram("vigil_db_write_ms", operation=operation).observe(
            ns_to_ms(self._clock.monotonic_ns() - started)
        )

    def _require_writer(self) -> sqlite3.Connection:
        if self._writer is None:
            raise PersistenceError("the incident database is not open")
        return self._writer

    def _rollback(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error as exc:
            self._log.warning("database_rollback_failed", error=str(exc))

    def _fail(self, operation: str, exc: sqlite3.Error) -> None:
        self._metrics.counter("vigil_db_errors_total", operation=operation).inc()
        self._log.error("database_write_failed", operation=operation, error=str(exc))
        raise PersistenceError(
            f"the incident database rejected a {operation}", context={"error": str(exc)}
        ) from exc

    # ---------------------------------------------------------------- queries

    def query(self, sql: str, params: Params = ()) -> list[Row]:
        """A parameterised read. Every caller passes values as parameters, never as text."""
        connection = self.reader()
        try:
            cursor = connection.execute(sql, params)
            try:
                return list(cursor.fetchall())
            finally:
                cursor.close()
        except sqlite3.Error as exc:
            self._metrics.counter("vigil_db_errors_total", operation="read").inc()
            raise PersistenceError(
                "the incident database rejected a read", context={"error": str(exc)}
            ) from exc

    def query_one(self, sql: str, params: Params = ()) -> Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Params = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    def schema_version(self) -> int:
        value = self.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_version")
        return int(value or 0)

    def known_schema_version(self) -> int:
        return MIGRATIONS[-1].version if MIGRATIONS else 0

    def size_bytes(self) -> int:
        try:
            return self._path.stat().st_size
        except OSError:
            return 0
