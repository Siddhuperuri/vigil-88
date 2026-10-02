"""Capture worker (07 §3): owns one source, stamps frames, publishes, reconnects.

Not a thread itself: `step()` is one deterministic unit of work (testable without threads),
and `run()` loops it. pipeline/workers.py runs `run()` on a thread. The worker does nothing
else: no preprocessing, no detection, no annotation.

Clock discipline (07 §4): a frame is stamped immediately after `read()` returns. Downstream
stages never read a clock for decisions.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from vigil.config.schema.pipeline import IngestConfig
from vigil.core.clock import Clock
from vigil.core.errors import SourceError, TransientSourceError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.protocols.source import FrameSource, SourceTiming
from vigil.domain.enums import CameraState
from vigil.domain.frame import Frame, FrameMeta
from vigil.ingest.backoff import reconnect_delay_ms
from vigil.ingest.buffers import FrameChannel, PutOutcome
from vigil.ingest.health import CameraHealthTracker
from vigil.observability.metrics import MetricsRegistry


class StepKind(StrEnum):
    OPENED = "opened"
    FRAME = "frame"
    GLITCH = "glitch"
    OPEN_FAILED = "open_failed"
    READ_FAILED = "read_failed"
    END_OF_STREAM = "end_of_stream"  # terminal
    FAILED = "failed"  # terminal
    STOPPED = "stopped"  # terminal


TERMINAL = frozenset({StepKind.END_OF_STREAM, StepKind.FAILED, StepKind.STOPPED})


@dataclass(frozen=True, slots=True)
class StepResult:
    kind: StepKind
    wait_ms: float = 0.0


class CaptureWorker:
    def __init__(
        self,
        *,
        camera_id: str,
        source: FrameSource,
        channel: FrameChannel,
        health: CameraHealthTracker,
        clock: Clock,
        ingest: IngestConfig,
        metrics: MetricsRegistry,
        rng: random.Random,
        logger: LoggerLike,
        seek: Callable[[float], None] | None = None,
    ) -> None:
        self._camera_id = camera_id
        self._source = source
        self._channel = channel
        self._health = health
        self._clock = clock
        self._cfg = ingest
        self._metrics = metrics
        self._rng = rng
        self._log = logger.bind(camera_id=camera_id, stage="ingest")
        self._seek = seek

        self._epoch = 0
        self._frame_index = 0
        self._dims: tuple[int, int] | None = None
        self._ever_online = False
        self._open_attempts = 0
        self._glitches = 0

    # ------------------------------------------------------------------ one step

    def step(self, stop: threading.Event) -> StepResult:
        if stop.is_set():
            return StepResult(StepKind.STOPPED)
        if not self._source.is_open:
            return self._open()
        return self._read(stop)

    def _open(self) -> StepResult:
        try:
            info = self._source.open()
        except SourceError as exc:
            return self._open_failed(exc)
        self._epoch += 1
        self._frame_index = 0
        self._dims = (info.width_px, info.height_px)
        self._ever_online = True
        self._open_attempts = 0
        self._glitches = 0
        self._health.mark_online(epoch=self._epoch, expected_fps=info.fps)
        self._log.info(
            "source opened",
            epoch=self._epoch,
            width_px=info.width_px,
            height_px=info.height_px,
            fps=info.fps,
            backend=info.backend,
            detail=info.detail,
        )
        return StepResult(StepKind.OPENED)

    def _open_failed(self, exc: SourceError) -> StepResult:
        self._open_attempts += 1
        attempts = self._open_attempts
        self._metrics.counter("vigil_source_open_failures_total", camera=self._camera_id).inc()
        self._log.warning("source open failed", attempt=attempts, error=exc.message)
        if not exc.retryable:
            self._health.mark_failed(exc.message)
            return StepResult(StepKind.FAILED)
        if self._gave_up(attempts):
            self._health.mark_failed(f"gave up after {attempts} attempts: {exc.message}")
            self._log.error("camera failed", attempts=attempts)
            return StepResult(StepKind.FAILED)
        self._health.mark_reconnecting(f"open failed (attempt {attempts}): {exc.message}")
        self._metrics.counter("vigil_reconnect_attempts_total", camera=self._camera_id).inc()
        return StepResult(StepKind.OPEN_FAILED, self._backoff(attempts - 1))

    def _gave_up(self, attempts: int) -> bool:
        if not self._ever_online:
            return attempts >= self._cfg.max_initial_open_attempts
        limit = self._cfg.max_reconnect_attempts
        return limit > 0 and attempts >= limit

    def _backoff(self, attempt: int) -> float:
        return reconnect_delay_ms(
            attempt,
            base_ms=self._cfg.reconnect_base_ms,
            max_ms=self._cfg.reconnect_max_ms,
            jitter_ratio=self._cfg.reconnect_jitter_ratio,
            rng=self._rng,
        )

    def _read(self, stop: threading.Event) -> StepResult:
        try:
            raw = self._source.read()
        except TransientSourceError as exc:
            return self._glitch(exc)
        except SourceError as exc:
            return self._read_failed(exc)

        if raw is None:
            self._health.mark_offline("end of stream")
            self._log.info("end of stream", frames=self._frame_index)
            return StepResult(StepKind.END_OF_STREAM)

        self._glitches = 0
        if self._seek is not None and raw.source_pts_ms is not None:
            self._seek(raw.source_pts_ms)
        t_mono = self._clock.monotonic_ns()  # stamp immediately after read() returns
        wall = self._clock.wall_utc()

        size = (raw.pixels.width_px, raw.pixels.height_px)
        if size != self._dims:
            self._on_format_change(size)
        meta = FrameMeta(
            camera_id=self._camera_id,
            stream_epoch=self._epoch,
            frame_index=self._frame_index,
            t_monotonic_ns=t_mono,
            wall_utc=wall,
            width_px=size[0],
            height_px=size[1],
            source_pts_ms=raw.source_pts_ms,
            is_keyframe=raw.is_keyframe,
        )
        self._frame_index += 1
        outcome = self._channel.put(Frame(meta, raw.pixels), stop)
        if outcome in (PutOutcome.STOPPED, PutOutcome.CLOSED):
            return StepResult(StepKind.STOPPED)
        if outcome is PutOutcome.OVERWROTE:
            self._health.record_skipped()
            self._metrics.counter("vigil_frames_skipped_total", camera=self._camera_id).inc()
        self._health.record_frame(t_mono, wall)
        self._metrics.counter("vigil_frames_received_total", camera=self._camera_id).inc()
        return StepResult(StepKind.FRAME)

    def _on_format_change(self, size: tuple[int, int]) -> None:
        """Resolution changed mid-stream: new epoch, so all downstream state is invalidated."""
        self._log.warning("stream format changed", old=self._dims, new=size)
        self._epoch += 1
        self._frame_index = 0
        self._dims = size
        self._health.mark_online(epoch=self._epoch, expected_fps=None)

    def _glitch(self, exc: TransientSourceError) -> StepResult:
        self._glitches += 1
        self._health.record_decode_error(self._clock.monotonic_ns())
        self._metrics.counter("vigil_decode_errors_total", camera=self._camera_id).inc()
        if self._glitches > self._cfg.max_consecutive_read_failures:
            self._log.warning("too many consecutive read failures", count=self._glitches)
            return self._read_failed(exc)
        return StepResult(StepKind.GLITCH, float(self._cfg.glitch_wait_ms))

    def _read_failed(self, exc: SourceError) -> StepResult:
        self._log.warning("source read failed", error=exc.message, retryable=exc.retryable)
        self._source.close()
        if not exc.retryable:
            self._health.mark_failed(exc.message)
            return StepResult(StepKind.FAILED)
        self._open_attempts = 0
        self._glitches = 0
        self._health.mark_reconnecting(f"read failed: {exc.message}")
        self._metrics.counter("vigil_reconnect_attempts_total", camera=self._camera_id).inc()
        return StepResult(StepKind.READ_FAILED, self._backoff(0))

    # ------------------------------------------------------------------ loop

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        try:
            while not stop.is_set():
                beat()
                result = self.step(stop)
                if result.kind in TERMINAL:
                    break
                if result.wait_ms > 0:
                    stop.wait(result.wait_ms / 1000.0)
        finally:
            self.close()

    def close(self) -> None:
        self._source.close()
        if self._health.state not in (CameraState.FAILED, CameraState.OFFLINE, CameraState.DISABLED):
            self._health.mark_offline("stopped")
