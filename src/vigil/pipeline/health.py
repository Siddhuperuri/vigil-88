"""Aggregate health (07 §9). Lives in pipeline/ because it combines domain CameraHealth with
worker health, and the observability layer sits below the domain."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from vigil.domain.camera import CameraHealth
from vigil.domain.enums import CameraState
from vigil.pipeline.workers import WorkerHealth


class HealthLevel(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HealthReport:
    level: HealthLevel
    issues: tuple[str, ...]


# States that are normal for a camera that is simply not streaming right now.
_QUIET = frozenset({CameraState.DISABLED, CameraState.OFFLINE})
_UNHEALTHY = frozenset(
    {
        CameraState.DEGRADED,
        CameraState.RECONNECTING,
        CameraState.ANALYSIS_SUSPENDED,
        CameraState.FAILED,
    }
)


def aggregate_health(
    cameras: Sequence[CameraHealth], workers: Sequence[WorkerHealth]
) -> HealthReport:
    issues: list[str] = []
    crashed = [w for w in workers if w.crashed]
    for w in crashed:
        issues.append(f"worker {w.name} crashed: {w.error}")
    for w in workers:
        if w.started and not w.alive and not w.crashed:
            continue  # a worker that finished cleanly (e.g. capture at end of stream)
    for c in cameras:
        if c.state in _UNHEALTHY:
            issues.append(f"camera {c.camera_id} is {c.state.value}: {c.detail or 'no detail'}")

    active = [c for c in cameras if c.state not in _QUIET]
    all_failed = bool(active) and all(c.state is CameraState.FAILED for c in active)
    if crashed or all_failed:
        level = HealthLevel.FAILED
    elif issues:
        level = HealthLevel.DEGRADED
    else:
        level = HealthLevel.OK
    return HealthReport(level, tuple(issues))
