"""Assembling the response half of the pipeline (01 §5).

`app.py` owns *lifecycle*; this file owns *composition*. Keeping them apart is what stops the
application object from growing a constructor that knows about SQLite pragmas, JPEG quality and
webhook timeouts all at once.

What gets built here, and the order it has to happen in:

  1. the database, opened and migrated, because every repository needs it;
  2. the repositories, which are the only things that speak SQL;
  3. the incident manager and the operator actions over them;
  4. the evidence ring per camera, the sampler that fills it, and the capture that drains it;
  5. the alert channels the operator actually listed;
  6. the response worker, and the retention sweep.

The whole stack is optional. With `storage.enabled = false` nothing here is built, the analysis
stage still runs, and the application reports that incidents are not being persisted -- which is
a legitimate configuration for a bench run, and must not be confused with a system that is
quietly losing them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from vigil.analytics.queries import AnalyticsQueries
from vigil.analytics.reports import ReportBuilder
from vigil.config.paths import ResolvedPaths
from vigil.config.schema.camera import CameraConfig
from vigil.config.settings import Settings
from vigil.core.clock import Clock
from vigil.core.ids import UlidFactory
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import MS_PER_S
from vigil.ingest.buffers import PreRollBuffer
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.database import Database
from vigil.persistence.migrations import MigrationReport
from vigil.persistence.repositories.events import CandidateRepository, HealthEventRepository
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.persistence.repositories.incidents import IncidentRepository
from vigil.persistence.repositories.metrics import CameraRepository, MetricRepository
from vigil.persistence.repositories.mutes import MuteRepository
from vigil.pipeline.analysis import AnalysisTick
from vigil.pipeline.evidence_capture import EvidenceCapture
from vigil.pipeline.preroll import PreRollSampler, build_buffers
from vigil.pipeline.queues import SpillQueue
from vigil.pipeline.response_worker import ResponseIdentity, ResponseWorker
from vigil.pipeline.results import LatestDetections
from vigil.response.channels.registry import AlertDispatcher, build_channels
from vigil.response.evidence import EvidenceWriter
from vigil.response.incident_manager import IncidentManager
from vigil.response.mutes import MuteRegistry
from vigil.response.operator_actions import OperatorActions
from vigil.response.retention import RetentionRepositories, RetentionWorker


@dataclass(frozen=True, slots=True)
class ResponseStack:
    """Everything P4 contributes, built once. Held by the application, read by the API."""

    database: Database
    migration: MigrationReport
    incidents: IncidentRepository
    evidence: EvidenceRepository
    candidates: CandidateRepository
    health_events: HealthEventRepository
    metric_rows: MetricRepository
    cameras: CameraRepository
    manager: IncidentManager
    actions: OperatorActions
    analytics: AnalyticsQueries
    reports: ReportBuilder
    mutes: MuteRegistry
    alerts: AlertDispatcher
    capture: EvidenceCapture
    sampler: PreRollSampler
    retention: RetentionWorker
    worker: ResponseWorker
    queue: SpillQueue[AnalysisTick]
    buffers: Mapping[str, PreRollBuffer]
    evidence_root: Path
    handoff_timeout_s: float

    def enqueue(self, tick: AnalysisTick) -> None:
        """The analysis stage's sink. Blocks briefly, then spills (01 §9).

        Called on an analysis thread, so the timeout is short: that thread owns a camera's
        detection, and waiting on storage there would stop it.
        """
        self.queue.put(tick, timeout_s=self.handoff_timeout_s)

    def close(self) -> None:
        self.database.close()


def build_response_stack(
    *,
    settings: Settings,
    paths: ResolvedPaths,
    cameras: Sequence[CameraConfig],
    results: LatestDetections,
    identity: ResponseIdentity,
    clock: Clock,
    ulids: UlidFactory,
    logger: LoggerLike,
    metrics: MetricsRegistry,
) -> ResponseStack:
    database = Database(
        paths.database_file,
        config=settings.storage,
        clock=clock,
        logger=logger,
        metrics=metrics,
    )
    migration = database.open()

    incidents = IncidentRepository(database)
    evidence_repo = EvidenceRepository(database)
    candidates = CandidateRepository(database)
    health_events = HealthEventRepository(database)
    metric_rows = MetricRepository(database)
    camera_rows = CameraRepository(database)
    mute_rows = MuteRepository(database)

    # Cameras are mirrored into the database so an incident stays describable after its camera
    # is renamed or removed (05 §4).
    camera_rows.upsert_all(cameras, now_wall_utc=clock.wall_utc())

    analytics = AnalyticsQueries(database)
    reports = ReportBuilder(analytics=analytics, incidents=incidents, evidence=evidence_repo)
    manager = IncidentManager(
        incidents=incidents,
        settings=settings,
        clock=clock,
        ulids=ulids,
        logger=logger,
        metrics=metrics,
    )
    actions = OperatorActions(
        manager=manager, incidents=incidents, clock=clock, logger=logger, metrics=metrics
    )
    mutes = MuteRegistry(
        repository=mute_rows, clock=clock, ulids=ulids, logger=logger, metrics=metrics
    )

    camera_ids = tuple(c.camera_id for c in cameras if c.enabled)
    buffers = build_buffers(camera_ids, settings.evidence)
    sampler = PreRollSampler(
        buffers=buffers,
        results=results,
        clock=clock,
        metrics=metrics,
        logger=logger,
        fps=settings.evidence.preroll_fps,
        jpeg_quality=settings.evidence.snapshot_quality,
    )
    writer = EvidenceWriter(
        evidence_root=paths.evidence_dir,
        retention=settings.retention,
        clock=clock,
        ulids=ulids,
        logger=logger,
        metrics=metrics,
        enabled=settings.evidence.enabled,
    )
    capture = EvidenceCapture(
        writer=writer,
        buffers=buffers,
        results=results,
        config=settings.evidence,
        clock=clock,
        logger=logger,
        metrics=metrics,
    )
    alerts = AlertDispatcher(
        channels=build_channels(settings.alerts, logger=logger, alerts_file=paths.alerts_file),
        config=settings.alerts,
        clock=clock,
        ulids=ulids,
        logger=logger,
        metrics=metrics,
    )

    # The queue is created before the worker, and the worker is its own spill sink: the spill
    # path only logs and counts, so there is no cycle at runtime.
    queue: SpillQueue[AnalysisTick] = SpillQueue(
        settings.pipeline.response_queue_size, spill=_no_sink
    )
    worker = ResponseWorker(
        queue=queue,
        incidents=manager,
        candidates=candidates,
        evidence=evidence_repo,
        capture=capture,
        alerts=alerts,
        identity=identity,
        settings=settings,
        clock=clock,
        logger=logger,
        metrics=metrics,
    )
    queue.set_spill(worker.spill)

    retention = RetentionWorker(
        repositories=RetentionRepositories(
            incidents=incidents,
            evidence=evidence_repo,
            candidates=candidates,
            metrics=metric_rows,
            health=health_events,
            mutes=mute_rows,
        ),
        config=settings.retention,
        evidence_root=paths.evidence_dir,
        clock=clock,
        logger=logger,
        metrics=metrics,
    )
    logger.info(
        "response_stack_ready",
        database=paths.database_file.name,
        schema_version=migration.version,
        cameras=len(camera_ids),
        evidence_enabled=settings.evidence.enabled,
        clips_enabled=settings.evidence.clips_enabled,
        alert_channels=list(alerts.channel_ids),
    )
    return ResponseStack(
        database=database,
        migration=migration,
        incidents=incidents,
        evidence=evidence_repo,
        candidates=candidates,
        health_events=health_events,
        metric_rows=metric_rows,
        cameras=camera_rows,
        manager=manager,
        actions=actions,
        analytics=analytics,
        reports=reports,
        mutes=mutes,
        alerts=alerts,
        capture=capture,
        sampler=sampler,
        retention=retention,
        worker=worker,
        queue=queue,
        buffers=buffers,
        evidence_root=paths.evidence_dir,
        handoff_timeout_s=settings.storage.handoff_timeout_ms / MS_PER_S,
    )


def _no_sink(_tick: AnalysisTick) -> None:
    """Placeholder until the worker exists; replaced immediately by `set_spill`."""
