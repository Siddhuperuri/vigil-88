"""The retention sweep (05 §8, 01 §10).

The default is finite, not infinite: a monitoring system that never deletes fills a disk and
then stops being a monitoring system. But deletion order is load-bearing, and the order below is
the one that cannot orphan a record:

  1. **Evidence files, then evidence rows.** File first: a row without its file makes the console
     offer a download that 404s, while a file without its row is an orphan this same sweep
     collects. Of the two inconsistencies, only one is visible to an operator.
  2. **Candidates**, which expire soonest because the volume is much higher than incidents.
  3. **Metrics**, downsampled to hourly averages *before* deletion, so a month-old trend
     survives at a fraction of the rows instead of vanishing.
  4. **Camera health events** and expired mutes.
  5. **Incidents last**, and only those whose evidence has already gone -- so an incident never
     references evidence that is not there.

Two incidents are never deleted automatically, whatever their age: one that is still open, and
one an operator annotated. An annotated incident is a human artifact, and deleting it would
throw away the only part of the record a person wrote.

Every run logs what it did, **including a run that deleted nothing**: silence is
indistinguishable from a broken worker.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar

from vigil.config.schema.retention import RetentionConfig
from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.core.protocols.logger import LoggerLike
from vigil.domain.evidence import Evidence
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.repositories.events import CandidateRepository, HealthEventRepository
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.persistence.repositories.incidents import IncidentRepository
from vigil.persistence.repositories.metrics import MetricRepository
from vigil.persistence.repositories.mutes import MuteRepository
from vigil.response.evidence import PART_SUFFIX
from vigil.response.paths import resolve_relative

# Each pass is bounded so a first run against a long-neglected database cannot hold the write
# lock for minutes. What is left over is simply collected by the next pass.
BATCH_LIMIT = 500
_MS_PER_S = 1000.0
_NS_PER_S = 1_000_000_000.0

# PEP 695 generics need 3.12; this project targets 3.11 (D-001).
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class RetentionReport:
    evidence_files_deleted: int = 0
    evidence_rows_deleted: int = 0
    evidence_bytes_reclaimed: int = 0
    evidence_files_missing: int = 0
    orphan_parts_deleted: int = 0
    candidates_deleted: int = 0
    metric_rows_downsampled: int = 0
    metric_rows_deleted: int = 0
    health_events_deleted: int = 0
    mutes_deleted: int = 0
    incidents_deleted: int = 0
    failures: tuple[str, ...] = ()

    @property
    def deleted_anything(self) -> bool:
        return any(
            (
                self.evidence_rows_deleted,
                self.candidates_deleted,
                self.metric_rows_deleted,
                self.health_events_deleted,
                self.mutes_deleted,
                self.incidents_deleted,
                self.orphan_parts_deleted,
            )
        )

    def as_fields(self) -> dict[str, object]:
        return {
            "evidence_files": self.evidence_files_deleted,
            "evidence_rows": self.evidence_rows_deleted,
            "evidence_bytes": self.evidence_bytes_reclaimed,
            "evidence_files_missing": self.evidence_files_missing,
            "orphan_parts": self.orphan_parts_deleted,
            "candidates": self.candidates_deleted,
            "metrics_downsampled": self.metric_rows_downsampled,
            "metrics_deleted": self.metric_rows_deleted,
            "health_events": self.health_events_deleted,
            "mutes": self.mutes_deleted,
            "incidents": self.incidents_deleted,
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class RetentionRepositories:
    incidents: IncidentRepository
    evidence: EvidenceRepository
    candidates: CandidateRepository
    metrics: MetricRepository
    health: HealthEventRepository
    mutes: MuteRepository


class RetentionWorker:
    """Runs the sweep on `retention.sweep_interval_s`. One per process."""

    __slots__ = ("_clock", "_config", "_evidence_root", "_log", "_metrics", "_repos")

    def __init__(
        self,
        *,
        repositories: RetentionRepositories,
        config: RetentionConfig,
        evidence_root: Path,
        clock: Clock,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._repos = repositories
        self._config = config
        self._evidence_root = evidence_root
        self._clock = clock
        self._log = logger.bind(component="retention")
        self._metrics = metrics

    # ---------------------------------------------------------------- one sweep

    def sweep(self) -> RetentionReport:
        now = self._clock.wall_utc()
        failures: list[str] = []
        evidence = self._sweep_evidence(now, failures)
        parts = self._sweep_orphan_parts(now, failures)
        candidates = self._guard(
            failures,
            "candidates",
            lambda: self._repos.candidates.delete_older_than(
                self._cutoff(now, self._config.candidate_days), limit=BATCH_LIMIT
            ),
            0,
        )
        downsampled, metrics_deleted = self._guard(
            failures,
            "metrics",
            lambda: self._repos.metrics.downsample_and_delete_older_than(
                self._cutoff(now, self._config.metric_days)
            ),
            (0, 0),
        )
        health = self._guard(
            failures,
            "health_events",
            lambda: self._repos.health.delete_older_than(
                self._cutoff(now, self._config.metric_days), limit=BATCH_LIMIT
            ),
            0,
        )
        mutes = self._guard(
            failures,
            "mutes",
            lambda: self._repos.mutes.delete_expired_before(
                self._cutoff(now, self._config.candidate_days), limit=BATCH_LIMIT
            ),
            0,
        )
        # Incidents last, and only ones whose evidence is already gone (the repository's query
        # enforces that, plus "not open" and "not annotated").
        incidents = self._guard(
            failures,
            "incidents",
            lambda: len(
                self._repos.incidents.delete_older_than(
                    self._cutoff(now, self._config.incident_days), limit=BATCH_LIMIT
                )
            ),
            0,
        )
        report = RetentionReport(
            evidence_files_deleted=evidence[0],
            evidence_rows_deleted=evidence[1],
            evidence_bytes_reclaimed=evidence[2],
            evidence_files_missing=evidence[3],
            orphan_parts_deleted=parts,
            candidates_deleted=candidates,
            metric_rows_downsampled=downsampled,
            metric_rows_deleted=metrics_deleted,
            health_events_deleted=health,
            mutes_deleted=mutes,
            incidents_deleted=incidents,
            failures=tuple(failures),
        )
        self._publish(report)
        return report

    def _cutoff(self, now: datetime, days: int) -> datetime:
        return now - timedelta(days=days)

    def _sweep_evidence(self, now: datetime, failures: list[str]) -> tuple[int, int, int, int]:
        """Returns (files deleted, rows deleted, bytes reclaimed, files already missing)."""
        try:
            expired = self._repos.evidence.expired(now, limit=BATCH_LIMIT)
        except PersistenceError as exc:
            failures.append(f"evidence listing failed: {exc.message}")
            return (0, 0, 0, 0)

        deleted_ids: list[str] = []
        files = bytes_freed = missing = 0
        for record in expired:
            try:
                path = resolve_relative(self._evidence_root, record.relative_path)
            except PersistenceError as exc:
                # A row whose path does not resolve safely is still deleted: leaving it would
                # keep offering a download the API would refuse anyway.
                failures.append(f"unsafe evidence path {record.evidence_id}: {exc.message}")
                deleted_ids.append(record.evidence_id)
                continue
            try:
                if path.exists():
                    path.unlink()
                    files += 1
                    bytes_freed += record.size_bytes
                else:
                    missing += 1
            except OSError as exc:
                # Keep the row: the file is still there and the next sweep will retry. Deleting
                # the row now would orphan the file forever.
                failures.append(f"cannot delete {record.relative_path}: {exc}")
                continue
            deleted_ids.append(record.evidence_id)

        rows = 0
        if deleted_ids:
            try:
                rows = self._repos.evidence.delete(deleted_ids)
            except PersistenceError as exc:
                failures.append(f"evidence row deletion failed: {exc.message}")
        self._prune_empty_dirs(expired)
        return (files, rows, bytes_freed, missing)

    def _prune_empty_dirs(self, records: Sequence[Evidence]) -> None:
        """Remove an incident's evidence directory once nothing is left in it."""
        seen: set[Path] = set()
        for record in records:
            try:
                directory = resolve_relative(self._evidence_root, record.relative_path).parent
            except PersistenceError:
                continue
            seen.add(directory)
        for directory in sorted(seen, key=lambda p: len(p.parts), reverse=True):
            try:
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError as exc:
                self._log.debug("evidence_dir_not_removed", dir=directory.name, error=str(exc))

    def _sweep_orphan_parts(self, now: datetime, failures: list[str]) -> int:
        """Collect `.part` files left behind by a write interrupted mid-flight.

        Only ones older than a sweep interval, so a capture in progress right now is never
        taken out from under the response worker.
        """
        horizon = now.timestamp() - self._config.sweep_interval_s
        removed = 0
        if not self._evidence_root.is_dir():
            return 0
        try:
            candidates = list(self._evidence_root.rglob(f"*{PART_SUFFIX}"))
        except OSError as exc:
            failures.append(f"cannot scan for interrupted writes: {exc}")
            return 0
        for path in candidates:
            try:
                if path.is_file() and path.stat().st_mtime < horizon:
                    path.unlink()
                    removed += 1
            except OSError as exc:
                failures.append(f"cannot delete interrupted write {path.name}: {exc}")
        return removed

    def _guard(self, failures: list[str], label: str, action: Callable[[], _T], fallback: _T) -> _T:
        """Run one step; a failure in one step must not abandon the others."""
        try:
            return action()
        except PersistenceError as exc:
            failures.append(f"{label} failed: {exc.message}")
            self._metrics.counter("vigil_retention_errors_total", step=label).inc()
            self._log.error("retention_step_failed", step=label, error=exc.message)
            return fallback

    def _publish(self, report: RetentionReport) -> None:
        self._metrics.counter("vigil_retention_runs_total").inc()
        self._metrics.counter("vigil_retention_evidence_deleted_total").inc(
            report.evidence_rows_deleted
        )
        self._metrics.counter("vigil_retention_bytes_reclaimed_total").inc(
            report.evidence_bytes_reclaimed
        )
        self._metrics.counter("vigil_retention_incidents_deleted_total").inc(
            report.incidents_deleted
        )
        if report.failures:
            self._log.warning("retention_completed_with_failures", **report.as_fields())
        else:
            # A run that deleted nothing is logged too: silence cannot be told apart from a
            # worker that has stopped running.
            self._log.info("retention_completed", **report.as_fields())

    # ---------------------------------------------------------------- the thread

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        interval_s = float(self._config.sweep_interval_s)
        while not stop.is_set():
            beat()
            started = self._clock.monotonic_ns()
            self.sweep()
            spent_s = (self._clock.monotonic_ns() - started) / _NS_PER_S
            self._metrics.histogram("vigil_retention_sweep_ms").observe(spent_s * _MS_PER_S)
            stop.wait(max(1.0, interval_s - spent_s))
