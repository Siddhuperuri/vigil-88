"""What the application does when a camera changes state."""

from __future__ import annotations

from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.core.protocols.logger import LoggerLike
from vigil.domain.camera import CameraStateChanged
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.response_stack import ResponseStack


def record_camera_state(
    event: CameraStateChanged,
    *,
    response: ResponseStack | None,
    metrics: MetricsRegistry,
    clock: Clock,
    logger: LoggerLike,
) -> None:
    metrics.counter(
        "vigil_camera_state_changes_total", camera=event.camera_id, state=event.new.value
    ).inc()
    logger.info(
        "camera state changed",
        camera_id=event.camera_id,
        old=event.old.value,
        new=event.new.value,
        detail=event.detail,
    )
    # Health events are persisted so the timeline can put them on the same axis as
    # incidents. "The camera was reconnecting when that fired" answers a large fraction of
    # "why did this fire" questions (08 §3).
    if response is None:
        return
    try:
        response.health_events.record(event.camera_id, event.new, clock.wall_utc(), event.detail)
    except PersistenceError as exc:
        metrics.counter("vigil_db_errors_total", operation="health_event").inc()
        logger.warning("camera_health_not_recorded", camera_id=event.camera_id, error=exc.message)
