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
from vigil.core.clock import Clock, SystemClock
from vigil.core.errors import (
    LifecycleError,
    PersistenceError,
    VigilError,
)
from vigil.core.ids import UlidFactory
from vigil.core.protocols.detector import Detector
from vigil.core.protocols.logger import LoggerLike
from vigil.core.rng import seeded_rng
from vigil.core.units import NS_PER_S, ns_to_ms
from vigil.domain.camera import Camera, CameraHealth, CameraStateChanged
from vigil.domain.enums import CameraState
from vigil.domain.incident import SystemSnapshot
from vigil.ingest.sources.factory import build_source
from vigil.ingest.watchdog import Watchdog
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.observability.probes import NvmlProbe, SystemProbe
from vigil.pipeline import shutdown
from vigil.pipeline.assembly import AnalysisAssembly
from vigil.pipeline.camera_runtime import CameraRuntime, SourceFactory, build_camera_runtime
from vigil.pipeline.cameras import camera_from_config
from vigil.pipeline.health import aggregate_health
from vigil.pipeline.inference import InferenceWorker
from vigil.pipeline.response_stack import ResponseStack
from vigil.pipeline.results import AnalysedFrame, LatestDetections
from vigil.pipeline.sampler import MetricsSampler
from vigil.pipeline.scheduler import CameraPlan, InferenceScheduler
from vigil.pipeline.snapshot import AppSnapshot, AppState, ShutdownReport
from vigil.pipeline.stages import Stages, build_stages
from vigil.pipeline.workers import ThreadWorker
from vigil.version import __version__
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
        self._cameras: dict[str, CameraRuntime] = {}
        self._system_workers: list[ThreadWorker] = []
        self._sampler: MetricsSampler | None = None
        self._shutdown: ShutdownReport | None = None
        self.ulids = UlidFactory(self.clock, seeded_rng(0, "incident-ids"))
        self.analysis: AnalysisAssembly | None = None
        self.response: ResponseStack | None = None
        self._stages: Stages | None = None

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
        # Health events are persisted so the timeline can put them on the same axis as
        # incidents. "The camera was reconnecting when that fired" answers a large fraction of
        # "why did this fire" questions (08 §3).
        response = self.response
        if response is None:
            return
        try:
            response.health_events.record(
                event.camera_id, event.new, self.clock.wall_utc(), event.detail
            )
        except PersistenceError as exc:
            self.metrics.counter("vigil_db_errors_total", operation="health_event").inc()
            self._log.warning(
                "camera_health_not_recorded", camera_id=event.camera_id, error=exc.message
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
            analysis_sink=self._analysis_sink,
            clock=self.clock,
            metrics=self.metrics,
            logger=self._log,
            max_batch_size=cfg.vision.max_batch_size,
            idle_wait_ms=cfg.pipeline.worker_idle_wait_ms,
            fps_window_s=cfg.observability.fps_window_s,
        )
        self._build_stages(detector)
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
        stage_workers: list[tuple[str, object]] = [
            ("inference", inference.run),
            ("watchdog", watchdog.run),
            ("sampler", self._sampler.run),
        ]
        if self.analysis is not None:
            stage_workers.append(("analysis", self.analysis.worker.run))
        if self.response is not None:
            stage_workers.extend(
                [
                    ("preroll", self.response.sampler.run),
                    ("response", self.response.worker.run),
                    ("retention", self.response.retention.run),
                ]
            )
        for name, target in stage_workers:
            self._system_workers.append(self._worker(name, target))  # type: ignore[arg-type]

        self._started_ns = self.clock.monotonic_ns()
        self._started_wall = self.clock.wall_utc()
        for w in self._system_workers:
            w.start()
        for rt in self._cameras.values():
            if rt.worker is not None:
                rt.worker.start()

        available = sorted(c.value for c in self.capabilities.available())
        stages = self._stages
        self._log.info(
            "application started",
            cameras=len(self._cameras),
            detector=detector.descriptor.name,
            capabilities_available=available,
            config_hash=cfg.config_hash(),
            modules_running=sorted(
                {
                    m
                    for r in (stages.analysis.bindings.values() if stages else ())
                    for m in r.runnable_ids
                }
            ),
            persists_incidents=bool(stages and stages.persists_incidents),
        )

    def _build_stages(self, detector: Detector) -> None:
        """Build the analysis and response stages. Composition lives in `stages.py` (01 §5)."""
        stages: Stages = build_stages(
            settings=self.settings,
            paths=self.paths,
            cameras=list(self.settings.cameras),
            detector_capabilities=frozenset(detector.provides),
            detector=lambda: detector.descriptor,
            system=self._system_snapshot,
            results=self.results,
            ledger=self.capabilities,
            clock=self.clock,
            ulids=self.ulids,
            logger=self._log,
            metrics=self.metrics,
            health_for=self._health_for,
        )
        self._stages = stages
        self.analysis = stages.analysis
        self.response = stages.response

    def _health_for(self, camera_id: str) -> CameraHealth | None:
        runtime = self._cameras.get(camera_id)
        return None if runtime is None else runtime.health.snapshot()

    def _system_snapshot(self) -> SystemSnapshot:
        """Copied onto every incident, so a past decision stays explainable (05 §4)."""
        latency = self.metrics.histogram("vigil_inference_latency_ms").summary()
        return SystemSnapshot(
            version=__version__,
            config_hash=self.settings.config_hash(),
            device=self._detector.descriptor.device if self._detector else "unknown",
            pipeline_fps=self.metrics.gauge("vigil_pipeline_fps").value,
            inference_latency_p95_ms=latency.p95,
        )

    def _analysis_sink(self, analysed: AnalysedFrame) -> None:
        """Hand an analysed frame to the analysis stage. Drop-oldest, per camera (01 §9)."""
        stages = self._stages
        if stages is None:
            return
        dropped = stages.analysis.queue.put(analysed.frame.meta.camera_id, analysed)
        if dropped is not None:
            self.metrics.counter(
                "vigil_frames_analysis_dropped_total", camera=dropped.camera_id
            ).inc(dropped.dropped)

    def _worker(
        self, name: str, target: Callable[[threading.Event, Callable[[], None]], None]
    ) -> ThreadWorker:
        return ThreadWorker(name, target, clock=self.clock, logger=self._log, metrics=self.metrics)

    def _build_camera(self, camera: Camera) -> CameraRuntime:
        """Composition lives in `camera_runtime.py`; this object owns lifecycle (01 §5)."""
        return build_camera_runtime(
            camera,
            settings=self.settings,
            clock=self.clock,
            bus=self.bus,
            metrics=self.metrics,
            logger=self._log,
            source_factory=self._source_factory,
            wake=self._wake,
            make_worker=self._worker,
        )

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
        shutdown.stop_workers(
            workers,
            [rt.channel for rt in self._cameras.values() if rt.channel is not None],
            close_hub=None if self.stream_hub is None else self.stream_hub.close,
        )
        stragglers = shutdown.join_workers(
            workers,
            timeout_ms=self.settings.pipeline.shutdown_timeout_ms,
            clock=self.clock,
        )
        shutdown.close_response(self.response, stragglers, logger=self._log, metrics=self.metrics)
        if self._detector is not None:
            self._detector.close()
        if self.nvml is not None and not stragglers:
            self.nvml.shutdown()  # only once no worker can still be reading it

        result = shutdown.report(stragglers, started_ns=started, clock=self.clock, logger=self._log)
        with self._lock:
            self._shutdown = result
            self._state = AppState.STOPPED
        return result

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
