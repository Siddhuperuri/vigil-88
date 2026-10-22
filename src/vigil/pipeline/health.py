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


def stalled_workers(
    workers: Sequence[WorkerHealth], *, stall_timeout_ms: float
) -> tuple[WorkerHealth, ...]:
    """Workers that are alive and have stopped making progress.

    Every worker beats once per loop iteration, so a beat that is minutes old means the thread
    is blocked, not busy — a decode that never returns (ADR 0011), a deadlock, a disk that has
    stopped answering. Until P6 the beacon was maintained and never read, so this case reported
    `alive=True, crashed=False` and the application's health stayed **OK** while the pipeline had
    quietly stopped working. A crash is at least visible; a stall was not.

    A worker that has *finished* (end of stream, a clean stop) is not stalled: it is not alive.
    """
    return tuple(
        w
        for w in workers
        if w.alive and w.heartbeat_age_ms is not None and w.heartbeat_age_ms > stall_timeout_ms
    )


def aggregate_health(
    cameras: Sequence[CameraHealth],
    workers: Sequence[WorkerHealth],
    *,
    worker_stall_timeout_ms: float,
) -> HealthReport:
    issues: list[str] = []
    crashed = [w for w in workers if w.crashed]
    for w in crashed:
        issues.append(f"worker {w.name} crashed: {w.error}")
    stalled = stalled_workers(workers, stall_timeout_ms=worker_stall_timeout_ms)
    for w in stalled:
        age_ms = w.heartbeat_age_ms or 0.0
        issues.append(
            f"worker {w.name} is alive but has not made progress for {age_ms / 1000:.0f} s; "
            "there is no automatic restart, so it needs one"
        )
    for c in cameras:
        if c.state in _UNHEALTHY:
            issues.append(f"camera {c.camera_id} is {c.state.value}: {c.detail or 'no detail'}")

    active = [c for c in cameras if c.state not in _QUIET]
    all_failed = bool(active) and all(c.state is CameraState.FAILED for c in active)
    # A stall is FAILED rather than DEGRADED for the same reason a crash is: part of the pipeline
    # has stopped doing its job and nothing in this build will restart it (LIMITATIONS).
    if crashed or stalled or all_failed:
        level = HealthLevel.FAILED
    elif issues:
        level = HealthLevel.DEGRADED
    else:
        level = HealthLevel.OK
    return HealthReport(level, tuple(issues))
