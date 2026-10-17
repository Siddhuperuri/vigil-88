"""Building one camera's ingest runtime (01 §4, 07 §3).

Split out of `app.py` because the application object owns *lifecycle* -- start, stop, report --
while this owns the decision of what a camera's capture path looks like. The two change for
different reasons: a new source kind touches this file, a new worker touches that one.

The shape of the path depends on one property of the source, its timing:

  * a **live** source gets a `LatestFrameSlot` and a stall watchdog. A three-second-old frame is
    worthless, so the slot overwrites and the overwrite is counted as *skipped* -- the system
    working as designed, kept distinct from *dropped* (01 §9).
  * a **paced** source (file replay) gets a bounded blocking queue and a `ReplayClock` driven by
    media timestamps. Replay must be lossless or results are not reproducible, and it must not
    be judged against the wall clock it is not running on.

A camera whose source cannot be built does not take the application down: it becomes FAILED with
the reason, and every other camera keeps running (01 §6).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from vigil.config.settings import Settings
from vigil.core.bus import EventBus
from vigil.core.clock import Clock, ReplayClock
from vigil.core.errors import CapabilityError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.protocols.source import FrameSource, SourceTiming
from vigil.core.rng import seeded_rng
from vigil.domain.camera import Camera
from vigil.domain.enums import CameraState
from vigil.ingest.buffers import BlockingFrameQueue, FrameChannel, LatestFrameSlot
from vigil.ingest.capture_worker import CaptureWorker
from vigil.ingest.health import CameraHealthTracker
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.workers import ThreadWorker

SourceFactory = Callable[[Camera], FrameSource]
WorkerFactory = Callable[[str, object], ThreadWorker]


@dataclass
class CameraRuntime:
    """One camera's ingest state. `channel` and `worker` are None when it cannot capture."""

    camera: Camera
    health: CameraHealthTracker
    channel: FrameChannel | None = None
    worker: ThreadWorker | None = None

    @property
    def captures(self) -> bool:
        return self.worker is not None and self.channel is not None


def build_camera_runtime(
    camera: Camera,
    *,
    settings: Settings,
    clock: Clock,
    bus: EventBus,
    metrics: MetricsRegistry,
    logger: LoggerLike,
    source_factory: SourceFactory,
    wake: threading.Event,
    make_worker: Callable[
        [str, Callable[[threading.Event, Callable[[], None]], None]], ThreadWorker
    ],
) -> CameraRuntime:
    """Build the capture path for one camera. Never raises for a camera that cannot start."""
    if not camera.enabled:
        return CameraRuntime(
            camera,
            CameraHealthTracker(
                camera.camera_id,
                clock=clock,
                ingest=settings.ingest,
                health=settings.health,
                bus=bus,
                initial_state=CameraState.DISABLED,
            ),
        )

    try:
        source = source_factory(camera)
    except CapabilityError as exc:
        tracker = CameraHealthTracker(
            camera.camera_id,
            clock=clock,
            ingest=settings.ingest,
            health=settings.health,
            bus=bus,
        )
        tracker.mark_failed(exc.message)
        metrics.counter("vigil_camera_unavailable_total", camera=camera.camera_id).inc()
        logger.error("camera cannot start", camera_id=camera.camera_id, reason=exc.message)
        return CameraRuntime(camera, tracker)

    paced = source.timing is not SourceTiming.LIVE
    cam_clock: Clock = clock
    seek: Callable[[float], None] | None = None
    if paced:
        replay = ReplayClock(wall_anchor=clock.wall_utc())
        cam_clock, seek = replay, replay.seek_pts_ms
    tracker = CameraHealthTracker(
        camera.camera_id,
        clock=cam_clock,
        ingest=settings.ingest,
        health=settings.health,
        bus=bus,
        watch_stall=not paced,
    )
    channel: FrameChannel = (
        BlockingFrameQueue(settings.pipeline.paced_queue_size, wake)
        if paced
        else LatestFrameSlot(wake)
    )
    capture = CaptureWorker(
        camera_id=camera.camera_id,
        source=source,
        channel=channel,
        health=tracker,
        clock=cam_clock,
        ingest=settings.ingest,
        metrics=metrics,
        rng=seeded_rng(0, f"capture:{camera.camera_id}"),
        logger=logger,
        seek=seek,
    )
    return CameraRuntime(
        camera, tracker, channel, make_worker(f"capture:{camera.camera_id}", capture.run)
    )
