"""Inference worker (04 §5): frames in, DetectionSets out.

Admission is decided by the InferenceScheduler (per-camera token buckets, a global ceiling,
priority with starvation protection); the worker takes frames only from cameras the scheduler
says may be served, batches up to `max_batch_size`, and speaks only the Detector protocol.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping

from vigil.core.clock import Clock
from vigil.core.errors import InferenceError
from vigil.core.protocols.detector import Detector
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import NS_PER_S, ns_to_ms
from vigil.domain.detection import PreparedFrame
from vigil.domain.frame import Frame
from vigil.ingest.buffers import FrameChannel
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.results import LatestDetections
from vigil.pipeline.scheduler import InferenceScheduler


class InferenceWorker:
    def __init__(
        self,
        *,
        detector: Detector,
        channels: Mapping[str, FrameChannel],
        scheduler: InferenceScheduler,
        wake: threading.Event,
        results: LatestDetections,
        clock: Clock,
        metrics: MetricsRegistry,
        logger: LoggerLike,
        max_batch_size: int,
        idle_wait_ms: int,
        fps_window_s: float,
    ) -> None:
        self._detector = detector
        self._channels = dict(channels)
        self._scheduler = scheduler
        self._wake = wake
        self._results = results
        self._clock = clock
        self._metrics = metrics
        self._log = logger.bind(stage="infer")
        self._max_batch = max_batch_size
        self._idle_s = idle_wait_ms / 1000.0
        self._fps_window_ns = int(fps_window_s * NS_PER_S)
        self._done_times: deque[int] = deque()

    def _collect(self) -> list[Frame]:
        batch: list[Frame] = []
        budget = self._scheduler.global_capacity()  # the global ceiling applies to live cameras
        for cam in self._scheduler.order():
            if len(batch) >= self._max_batch:
                break
            channel = self._channels[cam]
            if not channel.lossless and budget <= 0:
                continue
            frame = channel.take(0.0)
            if frame is None:
                continue
            self._scheduler.consume(cam)
            if not channel.lossless:
                budget -= 1
            batch.append(frame)
        return batch

    def step(self) -> int:
        """Process at most one batch. Returns the number of frames inferred."""
        batch = self._collect()
        if not batch:
            return 0
        try:
            outputs = self._detector.detect_batch([PreparedFrame(f) for f in batch])
        except InferenceError as exc:
            self._metrics.counter("vigil_inference_errors_total").inc()
            self._log.warning("inference failed", error=exc.message, batch=len(batch))
            return 0
        if len(outputs) != len(batch):
            self._metrics.counter("vigil_inference_errors_total").inc()
            self._log.error("detector broke its contract", expected=len(batch), got=len(outputs))
            return 0
        now = self._clock.monotonic_ns()
        for frame, result in zip(batch, outputs, strict=True):
            cam = frame.meta.camera_id
            self._results.publish(frame, result, now)
            self._metrics.counter("vigil_frames_inferred_total", camera=cam).inc()
            self._metrics.counter("vigil_detections_total", camera=cam).inc(len(result.detections))
            self._metrics.histogram("vigil_inference_latency_ms").observe(
                result.inference_latency_ms
            )
            if frame.meta.source_pts_ms is None:  # live: capture clock == this clock
                self._metrics.histogram("vigil_capture_to_infer_ms", camera=cam).observe(
                    ns_to_ms(now - frame.meta.t_monotonic_ns)
                )
            self._done_times.append(now)
        self._update_rate(now)
        return len(batch)

    def _update_rate(self, now_ns: int) -> None:
        while self._done_times and now_ns - self._done_times[0] > self._fps_window_ns:
            self._done_times.popleft()
        self._metrics.gauge("vigil_pipeline_fps").set(
            len(self._done_times) / (self._fps_window_ns / NS_PER_S)
        )
        for cam, channel in self._channels.items():
            self._metrics.gauge("vigil_queue_depth", queue=f"capture:{cam}").set(channel.depth())

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        """Sleep until a frame arrives or a token is due; never poll on a fixed interval.

        A fixed idle sleep after an empty poll silently caps throughput: service time plus the
        sleep can exceed the frame period, so every other frame is overwritten unseen."""
        while not stop.is_set():
            beat()
            self._wake.clear()  # before collecting, so a frame arriving during it is not lost
            if self.step() == 0:
                due_s = self._scheduler.seconds_until_ready()
                self._wake.wait(min(self._idle_s, due_s))
