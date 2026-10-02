"""Application lifecycle (headless core).

    CREATED -> STARTING -> RUNNING -> STOPPING -> STOPPED          (any step may -> FAILED)

Startup validates, builds the detector, grants capabilities from what the detector can
actually supply, builds one runtime per camera and starts the workers. A camera whose
source cannot be built (not implemented, extra missing) becomes FAILED with an explanation;
it does not take the application down. Shutdown requests stop on every worker first, then
joins them within `pipeline.shutdown_timeout_ms`, and reports any straggler by name.

The UI never reaches in here: it reads `snapshot()`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from vigil.config.paths import ResolvedPaths
from vigil.config.settings import Settings
from vigil.core.bus import EventBus
from vigil.core.capabilities import CapabilityLedger
from vigil.core.clock import Clock, ReplayClock, SystemClock
from vigil.core.errors import CapabilityError, LifecycleError, VigilError
from vigil.core.protocols.detector import Detector
from vigil.core.protocols.logger import LoggerLike
from vigil.core.protocols.source import FrameSource, SourceTiming
from vigil.core.rng import seeded_rng
from vigil.core.units import NS_PER_S, ns_to_ms
from vigil.domain.camera import Camera, CameraHealth, CameraStateChanged
from vigil.domain.enums import CameraState
from vigil.ingest.buffers import BlockingFrameQueue, FrameChannel, LatestFrameSlot
from vigil.ingest.capture_worker import CaptureWorker
from vigil.ingest.health import CameraHealthTracker
from vigil.ingest.sources.factory import build_source
from vigil.ingest.watchdog import Watchdog
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.observability.probes import NvmlProbe, SystemProbe
from vigil.pipeline.cameras import camera_from_config
from vigil.pipeline.health import aggregate_health
from vigil.pipeline.inference import InferenceWorker
from vigil.pipeline.results import LatestDetections
from vigil.pipeline.sampler import MetricsSampler
from vigil.pipeline.scheduler import CameraPlan, InferenceScheduler
from vigil.pipeline.snapshot import AppSnapshot, AppState, ShutdownReport
from vigil.pipeline.workers import ThreadWorker
from vigil.vision.factory import build_detector

if TYPE_CHECKING:
    from vigil.pipeline.streaming import StreamHub


@dataclass(frozen=True, slots=True)
class DetectorContext:
    """Everything a detector may need from the application, passed as one value so a factory
    (production or test) never needs the application itself."""

    settings: Settings
    paths: ResolvedPaths
    clock: Clock
    metrics: MetricsRegistry
    logger: LoggerLike
    nvml: NvmlProbe | None


DetectorFactory = Callable[[DetectorContext], Detector]
SourceFactory = Callable[[Camera], FrameSource]

_MS_PER_S = 1000.0


def _default_detector_factory(ctx: DetectorContext) -> Detector:
    return build_detector(
        ctx.settings.vision,
        ctx.clock,
        models_dir=ctx.paths.models_dir,
        logger=ctx.logger,
        metrics=ctx.metrics,
        nvml=ctx.nvml,
    )


@dataclass
class _CameraRuntime:
    camera: Camera
    health: CameraHealthTracker
    channel: FrameChannel | None = None
    worker: ThreadWorker | None = None


class Application:
    def __init__(
        self,
        settings: Settings,
        paths: ResolvedPaths,
        *,
        clock: Clock | None = None,
        detector_factory: DetectorFactory | None = None,
        source_factory: SourceFactory | None = None,
        stream_hub: StreamHub | None = None,
        bus: EventBus | None = None,
        metrics: MetricsRegistry | None = None,
        logger: LoggerLike | None = None,
    ) -> None:
        self.settings = settings
        self.paths = paths
        self.clock: Clock = clock or SystemClock()
        self.metrics = metrics or MetricsRegistry(
            histogram_samples=settings.observability.histogram_samples
        )
        self.bus = bus or EventBus(on_handler_error=self._on_bus_error)
        self.capabilities = CapabilityLedger()
        self.results = LatestDetections()
        self._log = logger or get_logger("pipeline.app")
        self._detector_factory = detector_factory or _default_detector_factory
        self._source_factory = source_factory or (
            lambda camera: build_source(camera.source, self.clock)
        )
        self.stream_hub = stream_hub
        self._wake = threading.Event()  # set by every channel put; wakes the inference worker

        self._lock = threading.Lock()
        self._state = AppState.CREATED
        self._started_ns: int | None = None
        self._started_wall: datetime | None = None
        self._detector: Detector | None = None
        self.nvml: NvmlProbe | None = None
        self._cameras: dict[str, _CameraRuntime] = {}
        self._system_workers: list[ThreadWorker] = []
        self._sampler: MetricsSampler | None = None
        self._shutdown: ShutdownReport | None = None

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    def _set_state(self, new: AppState) -> None:
        with self._lock:
            self._state = new

    def _on_bus_error(self, event: object, exc: BaseException) -> None:
        self.metrics.counter("vigil_bus_handler_errors_total").inc()
        self._log.warning(
            "event bus subscriber failed", event_type=type(event).__name__, error=repr(exc)
        )

    def _on_camera_state(self, event: CameraStateChanged) -> None:
        self.metrics.counter(
            "vigil_camera_state_changes_total", camera=event.camera_id, state=event.new.value
        ).inc()
        self._log.info(
            "camera state changed",
            camera_id=event.camera_id,
            old=event.old.value,
            new=event.new.value,
            detail=event.detail,
        )

    # ------------------------------------------------------------------ startup

    def start(self) -> None:
        with self._lock:
            if self._state is not AppState.CREATED:
                raise LifecycleError(f"cannot start an application in state {self._state.value}")
            self._state = AppState.STARTING
        try:
            self._start()
        except (VigilError, OSError):
            self._set_state(AppState.FAILED)
            self._abort_startup()
            raise
        self._set_state(AppState.RUNNING)

    def _start(self) -> None:
        cfg = self.settings
        self.paths.ensure_directories()
        self.bus.subscribe(CameraStateChanged, self._on_camera_state)

        probe = NvmlProbe()
        self.nvml = probe if probe.available else None
        if self.nvml is None:
            self._log.info("GPU metrics unavailable", reason=probe.reason)
        detector = self._detector_factory(
            DetectorContext(cfg, self.paths, self.clock, self.metrics, self._log, self.nvml)
        )
        self._detector = detector
        detector.warmup()
        for capability in detector.provides:
            self.capabilities.grant(capability, detector.descriptor.name)

        for cam_cfg in cfg.cameras:
            camera = camera_from_config(cam_cfg)
            self._cameras[camera.camera_id] = self._build_camera(camera)

        channels = {cid: rt.channel for cid, rt in self._cameras.items() if rt.channel is not None}
        plans = [
            CameraPlan(cid, rt.camera.target_inference_fps, rt.camera.priority, rt.channel.lossless)
            for cid, rt in self._cameras.items()
            if rt.channel is not None
        ]
        scheduler = InferenceScheduler(
            plans,
            global_fps=cfg.pipeline.global_inference_fps,
            global_burst=cfg.vision.max_batch_size,
            clock=self.clock,
        )
        inference = InferenceWorker(
            detector=detector,
            channels=channels,
            scheduler=scheduler,
            wake=self._wake,
            results=self.results,
            clock=self.clock,
            metrics=self.metrics,
            logger=self._log,
            max_batch_size=cfg.vision.max_batch_size,
            idle_wait_ms=cfg.pipeline.worker_idle_wait_ms,
            fps_window_s=cfg.observability.fps_window_s,
        )
        trackers = [rt.health for rt in self._cameras.values()]
        watchdog = Watchdog(trackers, interval_ms=cfg.ingest.watchdog_interval_ms)
        obs = cfg.observability
        history_len = max(1, round(obs.series_window_s * _MS_PER_S / obs.sample_interval_ms))
        self._sampler = MetricsSampler(
            probe=SystemProbe(gpu=self.nvml),
            metrics=self.metrics,
            clock=self.clock,
            interval_ms=cfg.observability.sample_interval_ms,
            history_len=history_len,
        )
        if self.stream_hub is not None:
            from vigil.pipeline.streaming import StreamWorker  # imports OpenCV: only when streaming

            stream = StreamWorker(
                hub=self.stream_hub,
                results=self.results,
                clock=self.clock,
                metrics=self.metrics,
                logger=self._log,
                fps=cfg.api.stream_fps,
                jpeg_quality=cfg.api.stream_jpeg_quality,
            )
            self._system_workers.append(self._worker("stream", stream.run))
        for name, target in (
            ("inference", inference.run),
            ("watchdog", watchdog.run),
            ("sampler", self._sampler.run),
        ):
            self._system_workers.append(self._worker(name, target))

        self._started_ns = self.clock.monotonic_ns()
        self._started_wall = self.clock.wall_utc()
        for w in self._system_workers:
            w.start()
        for rt in self._cameras.values():
            if rt.worker is not None:
                rt.worker.start()

        available = sorted(c.value for c in self.capabilities.available())
        self._log.info(
            "application started",
            cameras=len(self._cameras),
            detector=detector.descriptor.name,
            capabilities_available=available,
            config_hash=cfg.config_hash(),
        )

    def _worker(
        self, name: str, target: Callable[[threading.Event, Callable[[], None]], None]
    ) -> ThreadWorker:
        return ThreadWorker(name, target, clock=self.clock, logger=self._log, metrics=self.metrics)

    def _build_camera(self, camera: Camera) -> _CameraRuntime:
        cfg = self.settings
        if not camera.enabled:
            tracker = CameraHealthTracker(
                camera.camera_id,
                clock=self.clock,
                ingest=cfg.ingest,
                health=cfg.health,
                bus=self.bus,
                initial_state=CameraState.DISABLED,
            )
            return _CameraRuntime(camera, tracker)

        try:
            source = self._source_factory(camera)
        except CapabilityError as exc:
            tracker = CameraHealthTracker(
                camera.camera_id,
                clock=self.clock,
                ingest=cfg.ingest,
                health=cfg.health,
                bus=self.bus,
            )
            tracker.mark_failed(exc.message)
            self.metrics.counter("vigil_camera_unavailable_total", camera=camera.camera_id).inc()
            self._log.error("camera cannot start", camera_id=camera.camera_id, reason=exc.message)
            return _CameraRuntime(camera, tracker)

        paced = source.timing is not SourceTiming.LIVE
        cam_clock: Clock = self.clock
        seek: Callable[[float], None] | None = None
        if paced:
            replay = ReplayClock(wall_anchor=self.clock.wall_utc())
            cam_clock, seek = replay, replay.seek_pts_ms
        tracker = CameraHealthTracker(
            camera.camera_id,
            clock=cam_clock,
            ingest=cfg.ingest,
            health=cfg.health,
            bus=self.bus,
            watch_stall=not paced,
        )
        channel: FrameChannel = (
            BlockingFrameQueue(cfg.pipeline.paced_queue_size, self._wake)
            if paced
            else LatestFrameSlot(self._wake)
        )
        capture = CaptureWorker(
            camera_id=camera.camera_id,
            source=source,
            channel=channel,
            health=tracker,
            clock=cam_clock,
            ingest=cfg.ingest,
            metrics=self.metrics,
            rng=seeded_rng(0, f"capture:{camera.camera_id}"),
            logger=self._log,
            seek=seek,
        )
        worker = self._worker(f"capture:{camera.camera_id}", capture.run)
        return _CameraRuntime(camera, tracker, channel, worker)

    def _abort_startup(self) -> None:
        for rt in self._cameras.values():
            if rt.channel is not None:
                rt.channel.close()
        if self._detector is not None:
            self._detector.close()
        if self.nvml is not None:
            self.nvml.shutdown()

    # ------------------------------------------------------------------ shutdown

    def stop(self) -> ShutdownReport:
        """Idempotent. Returns the same report if already stopped."""
        with self._lock:
            if self._shutdown is not None:
                return self._shutdown
            if self._state in (AppState.CREATED, AppState.FAILED):
                self._state = AppState.STOPPED
                self._shutdown = ShutdownReport(True, (), 0.0)
                return self._shutdown
            if self._state is AppState.STOPPING:
                raise LifecycleError("shutdown already in progress")
            self._state = AppState.STOPPING

        started = self.clock.monotonic_ns()
        workers = [rt.worker for rt in self._cameras.values() if rt.worker is not None]
        workers += self._system_workers
        for w in workers:
            w.request_stop()
        for rt in self._cameras.values():
            if rt.channel is not None:
                rt.channel.close()  # unblocks a capture worker waiting to hand off
        if self.stream_hub is not None:
            self.stream_hub.close()  # wakes any connected viewer so it can disconnect

        timeout_s = self.settings.pipeline.shutdown_timeout_ms / _MS_PER_S
        deadline_ns = self.clock.monotonic_ns() + int(timeout_s * NS_PER_S)
        stragglers: list[str] = []
        for w in workers:
            remaining = max(0.0, (deadline_ns - self.clock.monotonic_ns()) / NS_PER_S)
            if not w.join(remaining):
                stragglers.append(w.name)
        if self._detector is not None:
            self._detector.close()
        if self.nvml is not None and not stragglers:
            self.nvml.shutdown()  # only once no worker can still be reading it

        report = ShutdownReport(
            clean=not stragglers,
            stragglers=tuple(stragglers),
            duration_ms=ns_to_ms(self.clock.monotonic_ns() - started),
        )
        if stragglers:
            self._log.error("shutdown incomplete", stragglers=stragglers)
        else:
            self._log.info("application stopped", duration_ms=round(report.duration_ms, 1))
        with self._lock:
            self._shutdown = report
            self._state = AppState.STOPPED
        return report

    def __enter__(self) -> Application:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ queries

    def camera_health(self) -> tuple[CameraHealth, ...]:
        return tuple(rt.health.snapshot() for rt in self._cameras.values())

    def wait_until_drained(self, timeout_s: float) -> bool:
        """True once every camera has finished and every frame it produced was inferred.

        Meaningful for PACED sources (nothing skipped). Polls; used by selftest and tests.
        """
        stop = threading.Event()
        deadline = self.clock.monotonic_ns() + int(timeout_s * NS_PER_S)
        while self.clock.monotonic_ns() < deadline:
            if self._drained():
                return True
            stop.wait(0.01)
        return self._drained()

    def _drained(self) -> bool:
        for rt in self._cameras.values():
            if rt.worker is None:
                continue
            h = rt.health.snapshot()
            if h.state not in (CameraState.OFFLINE, CameraState.FAILED):
                return False
            if rt.channel is not None and rt.channel.depth() > 0:
                return False
            inferred = self.metrics.counter(
                "vigil_frames_inferred_total", camera=rt.camera.camera_id
            ).value
            if inferred < h.frames_received - h.frames_skipped:
                return False
        return True

    def snapshot(self) -> AppSnapshot:
        cameras = self.camera_health()
        workers = tuple(
            w.health()
            for w in (
                *[rt.worker for rt in self._cameras.values() if rt.worker is not None],
                *self._system_workers,
            )
        )
        uptime = (
            None
            if self._started_ns is None
            else ns_to_ms(self.clock.monotonic_ns() - self._started_ns)
        )
        return AppSnapshot(
            state=self.state,
            started_wall_utc=self._started_wall,
            uptime_ms=uptime,
            cameras=cameras,
            workers=workers,
            capabilities=self.capabilities.table(),
            health=aggregate_health(cameras, workers),
            detector=self._detector.descriptor if self._detector else None,
            frames_inferred=int(self.metrics.counter_total("vigil_frames_inferred_total")),
            detections_total=int(self.metrics.counter_total("vigil_detections_total")),
            system=self._sampler.latest() if self._sampler else None,
        )
