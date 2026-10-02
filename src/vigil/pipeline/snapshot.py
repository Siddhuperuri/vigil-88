"""Immutable application snapshot: what the API/CLI reads, never live pipeline state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from vigil.core.capabilities import CapabilityStatus
from vigil.domain.camera import CameraHealth
from vigil.domain.detection import ModelDescriptor
from vigil.domain.metric import SystemMetric
from vigil.pipeline.health import HealthReport
from vigil.pipeline.workers import WorkerHealth


class AppState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    clean: bool
    stragglers: tuple[str, ...]  # workers that did not stop within the timeout
    duration_ms: float


@dataclass(frozen=True, slots=True)
class AppSnapshot:
    state: AppState
    started_wall_utc: datetime | None
    uptime_ms: float | None
    cameras: tuple[CameraHealth, ...]
    workers: tuple[WorkerHealth, ...]
    capabilities: tuple[CapabilityStatus, ...]
    health: HealthReport
    detector: ModelDescriptor | None  # None until started
    frames_inferred: int
    detections_total: int
    system: SystemMetric | None
