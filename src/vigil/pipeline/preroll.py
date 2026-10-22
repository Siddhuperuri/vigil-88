"""Keeping each camera's evidence ring filled (05 §6, Q3).

Evidence has to begin *before* the event that caused it, which means frames must already be in
memory when a decision is made. This worker is what puts them there: it samples the latest
analysed frame per camera at `evidence.preroll_fps`, JPEG-encodes it, and appends it to that
camera's bounded ring.

Why a worker of its own rather than part of the response worker: the response worker spends
hundreds of milliseconds muxing a clip and writing to disk, and a hole in the pre-roll for the
duration of every capture would be a hole in exactly the footage someone wants. Sampling is
cheap and periodic; writing is expensive and bursty. They do not belong on one thread.

Why it samples rather than being pushed to: capture and inference must never block on evidence
(01 §6). A sampler that misses a frame costs one frame of pre-roll; a push that blocked the
inference worker would cost detection on every camera.

The ring is sized to span pre-roll **and** post-roll, so one buffer serves both: at capture time
the response worker slices `[first_observed - preroll, confirmed + postroll]` out of it. That is
why the buffer is read long after an incident is confirmed, and why the byte ceiling
(`evidence.preroll_max_mb`) is the bound that actually matters.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from vigil.config.schema.pipeline import EvidenceConfig
from vigil.core.clock import Clock
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import BYTES_PER_MIB, MS_PER_S, NS_PER_S
from vigil.ingest.buffers import EncodedFrame, PreRollBuffer
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.annotate import OverlayOptions, RenderError, render_jpeg
from vigil.pipeline.results import LatestDetections

# Pre-roll frames are the unannotated record of what the camera saw. Boxes belong on the
# annotated snapshot, which is rendered separately: an operator reviewing evidence needs to be
# able to see the scene without our interpretation drawn over it.
RAW = OverlayOptions(boxes=False, labels=False, info=False)


def buffer_window_ms(config: EvidenceConfig) -> float:
    """One ring spans pre-roll plus post-roll, plus a frame's slack at either end."""
    return (config.preroll_seconds + config.postroll_seconds) * MS_PER_S


def build_buffers(camera_ids: tuple[str, ...], config: EvidenceConfig) -> dict[str, PreRollBuffer]:
    """One ring per camera, each independently capped, so a busy camera cannot starve a quiet
    one of evidence memory."""
    return {
        camera_id: PreRollBuffer(
            window_ms=buffer_window_ms(config),
            max_bytes=config.preroll_max_mb * BYTES_PER_MIB,
        )
        for camera_id in camera_ids
    }


class PreRollSampler:
    __slots__ = (
        "_buffers",
        "_clock",
        "_epochs",
        "_interval_s",
        "_log",
        "_metrics",
        "_quality",
        "_results",
        "_versions",
    )

    def __init__(
        self,
        *,
        buffers: Mapping[str, PreRollBuffer],
        results: LatestDetections,
        clock: Clock,
        metrics: MetricsRegistry,
        logger: LoggerLike,
        fps: float,
        jpeg_quality: int,
    ) -> None:
        self._buffers = dict(buffers)
        self._results = results
        self._clock = clock
        self._metrics = metrics
        self._log = logger.bind(stage="preroll")
        self._interval_s = 1.0 / max(fps, 0.1)
        self._quality = jpeg_quality
        self._versions: dict[str, int] = {}
        self._epochs: dict[str, int] = {}

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._buffers))

    def buffer_for(self, camera_id: str) -> PreRollBuffer | None:
        return self._buffers.get(camera_id)

    def total_bytes(self) -> int:
        return sum(b.size_bytes for b in self._buffers.values())

    # ---------------------------------------------------------------- one pass

    def step(self) -> int:
        """Encode and store one frame per camera that has a new one. Returns the count."""
        stored = 0
        for camera_id in self.camera_ids:
            version = self._results.version(camera_id)
            if version == self._versions.get(camera_id):
                continue  # nothing new: a camera at 5 fps must not be re-encoded at 10
            analysed = self._results.latest(camera_id)
            if analysed is None:
                continue
            self._versions[camera_id] = version
            buffer = self._buffers[camera_id]
            self._reset_on_epoch_change(camera_id, analysed.frame.meta.stream_epoch, buffer)
            try:
                jpeg = render_jpeg(analysed, RAW, jpeg_quality=self._quality, age_ms=None)
            except RenderError as exc:
                self._metrics.counter("vigil_preroll_encode_errors_total", camera=camera_id).inc()
                self._log.warning("preroll_encode_failed", camera_id=camera_id, error=exc.message)
                continue
            buffer.append(EncodedFrame(meta=analysed.frame.meta, jpeg=jpeg))
            stored += 1
            self._publish(camera_id, buffer)
        return stored

    def _reset_on_epoch_change(
        self, camera_id: str, stream_epoch: int, buffer: PreRollBuffer
    ) -> None:
        """A reconnect may mean a different view, and presenting pre-reconnect frames as this
        incident's pre-roll would be misleading."""
        previous = self._epochs.get(camera_id)
        self._epochs[camera_id] = stream_epoch
        if previous is not None and previous != stream_epoch:
            buffer.clear()
            self._log.info(
                "preroll_cleared_on_epoch_change", camera_id=camera_id, stream_epoch=stream_epoch
            )

    def _publish(self, camera_id: str, buffer: PreRollBuffer) -> None:
        self._metrics.gauge("vigil_preroll_bytes", camera=camera_id).set(float(buffer.size_bytes))
        self._metrics.gauge("vigil_preroll_frames", camera=camera_id).set(float(buffer.frame_count))
        self._metrics.gauge("vigil_preroll_span_ms", camera=camera_id).set(buffer.span_ms)

    # ---------------------------------------------------------------- the thread

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        while not stop.is_set():
            beat()
            started = self._clock.monotonic_ns()
            self.step()
            spent_s = (self._clock.monotonic_ns() - started) / NS_PER_S
            stop.wait(max(0.001, self._interval_s - spent_s))
