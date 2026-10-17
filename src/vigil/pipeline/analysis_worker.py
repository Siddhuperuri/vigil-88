"""The analysis stage: analysed frames in, decided events out (01 §4, §8).

One thread serves every camera, and a camera's state is touched only by this thread (D-005), so
the tracker, the scene analyzer and the accumulator store need no locks. Cameras are drained in
rotation, so a fast camera cannot starve a slow one.

The stage order is fixed here because this is L5, where orchestration belongs (01 §5):

    frame + detections -> track -> scene -> observe -> accumulate -> promote -> verify -> score

and the one thing that must not exist is a shortcut past any of those arrows. `AnalysisPipeline`
owns the second half; this worker owns the first two stages and the hand-off.

Nothing here writes a file or a database row. The decided tick goes to a bounded queue and the
response worker does that work, because an evidence write or a `fsync` on this thread would
stall detection on every camera (01 §6: Response fails *open*, Analysis must never block).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from vigil.core.capabilities import Capability
from vigil.core.clock import Clock
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import ns_to_ms
from vigil.domain.camera import CameraHealth
from vigil.domain.mute import Mute
from vigil.events.binding import BindingReport
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.analysis import AnalysisPipeline, AnalysisTick
from vigil.pipeline.queues import RoundRobinDropQueue
from vigil.pipeline.results import AnalysedFrame
from vigil.scene.analyzer import SceneAnalyzer
from vigil.tracking.bytetrack import ByteTracker

HealthLookup = Callable[[str], CameraHealth | None]
MuteLookup = Callable[[str], Sequence[Mute]]
TickSink = Callable[[AnalysisTick], None]

_MS_PER_S = 1000.0


@dataclass(slots=True)
class CameraAnalysis:
    """Everything one camera needs to turn a frame into decided events."""

    camera_id: str
    tracker: ByteTracker
    analyzer: SceneAnalyzer
    pipeline: AnalysisPipeline
    binding: BindingReport
    capabilities: frozenset[Capability]
    last_tracker_epoch: int | None = None
    frames_analysed: int = 0

    @property
    def runnable_module_ids(self) -> tuple[str, ...]:
        return self.binding.runnable_ids

    def release(self) -> None:
        self.pipeline.release()


@dataclass(slots=True)
class AnalysisStats:
    frames: int = 0
    observations: int = 0
    events: int = 0
    rejections: int = 0
    by_camera: dict[str, int] = field(default_factory=dict)


class AnalysisWorker:
    def __init__(
        self,
        *,
        cameras: Mapping[str, CameraAnalysis],
        queue: RoundRobinDropQueue[AnalysedFrame],
        sink: TickSink,
        health_for: HealthLookup,
        mutes_for: MuteLookup,
        clock: Clock,
        metrics: MetricsRegistry,
        logger: LoggerLike,
        idle_wait_ms: int,
    ) -> None:
        self._cameras = dict(cameras)
        self._queue = queue
        self._sink = sink
        self._health_for = health_for
        self._mutes_for = mutes_for
        self._clock = clock
        self._metrics = metrics
        self._log = logger.bind(stage="analyse")
        self._idle_s = idle_wait_ms / _MS_PER_S
        self.stats = AnalysisStats()

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._cameras))

    def runtime(self, camera_id: str) -> CameraAnalysis | None:
        return self._cameras.get(camera_id)

    # ---------------------------------------------------------------- one frame

    def step(self) -> int:
        """Analyse at most one queued frame. Returns the number analysed (0 or 1)."""
        analysed = self._queue.take(0.0)
        if analysed is None:
            return 0
        runtime = self._cameras.get(analysed.frame.meta.camera_id)
        if runtime is None:
            return 0  # a camera removed while its frame was in flight
        self._analyse(runtime, analysed)
        return 1

    def _analyse(self, runtime: CameraAnalysis, analysed: AnalysedFrame) -> None:
        started_ns = self._clock.monotonic_ns()
        meta = analysed.frame.meta
        tracks = runtime.tracker.update(analysed.result, meta)

        if runtime.last_tracker_epoch != runtime.tracker.epoch:
            # The scene analyzer keeps membership and dwell keyed by track id; across an epoch
            # those ids describe different objects, so the history is worse than useless.
            runtime.analyzer.reset()
            runtime.last_tracker_epoch = runtime.tracker.epoch

        scene = runtime.analyzer.analyze(
            tracks,
            meta,
            pixels=analysed.frame.pixels,
            capabilities=runtime.capabilities,
        )
        runtime.pipeline.set_camera_health(self._health_for(runtime.camera_id))
        runtime.pipeline.set_mutes(self._mutes_for(runtime.camera_id))
        tick = runtime.pipeline.process(scene)

        runtime.frames_analysed += 1
        self._record(runtime, tick, started_ns, analysed)
        if tick.has_work:
            self._sink(tick)

    def _record(
        self,
        runtime: CameraAnalysis,
        tick: AnalysisTick,
        started_ns: int,
        analysed: AnalysedFrame,
    ) -> None:
        camera_id = runtime.camera_id
        latency_ms = ns_to_ms(self._clock.monotonic_ns() - started_ns)
        self._metrics.histogram(
            "vigil_stage_latency_ms", stage="analyse", camera=camera_id
        ).observe(latency_ms)
        self._metrics.counter("vigil_frames_analysed_total", camera=camera_id).inc()
        self._metrics.gauge("vigil_queue_depth", queue=f"analysis:{camera_id}").set(
            float(self._queue.depth(camera_id))
        )
        self.stats.frames += 1
        self.stats.observations += len(tick.observations)
        self.stats.events += len(tick.scored)
        self.stats.rejections += len(tick.rejected)
        self.stats.by_camera[camera_id] = self.stats.by_camera.get(camera_id, 0) + 1
        for failure in tick.module_failures:
            self._metrics.counter(
                "vigil_module_failures_total", camera=camera_id, module=failure.module_id
            ).inc()
        if tick.scored:
            # Capture-to-decision, not inference-to-decision: an operator cares how long after
            # the event happened the system knew (live sources only; a replay clock is not
            # comparable to the application clock).
            if analysed.frame.meta.source_pts_ms is None:
                self._metrics.histogram("vigil_event_latency_ms", camera=camera_id).observe(
                    ns_to_ms(self._clock.monotonic_ns() - analysed.frame.meta.t_monotonic_ns)
                )
            for scored in tick.scored:
                self._metrics.counter(
                    "vigil_events_verified_total",
                    camera=camera_id,
                    event_type=scored.candidate.event_type.value,
                ).inc()
                self._metrics.histogram("vigil_detection_latency_ms", camera=camera_id).observe(
                    scored.detection_latency_ms
                )

    # ---------------------------------------------------------------- the thread

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        while not stop.is_set():
            beat()
            if self.step() == 0:
                queued = self._queue.take(self._idle_s)
                if queued is None:
                    continue
                runtime = self._cameras.get(queued.frame.meta.camera_id)
                if runtime is not None:
                    self._analyse(runtime, queued)
        self._drain_remaining()

    def _drain_remaining(self) -> None:
        """At shutdown, analyse nothing further: queued frames are evidence of load, not work
        that must complete. Depths are published so the gap is visible rather than silent."""
        for camera_id in self._queue.keys():  # noqa: SIM118 - a method, not a mapping
            depth = self._queue.depth(camera_id)
            if depth:
                self._metrics.counter(
                    "vigil_frames_unanalysed_at_shutdown_total", camera=camera_id
                ).inc(depth)
                self._log.info("analysis_queue_abandoned", camera_id=camera_id, frames=depth)

    def release(self) -> None:
        for camera_id in self.camera_ids:
            self._cameras[camera_id].release()
