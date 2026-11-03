"""Live stream plumbing (08 §4): the hub that viewers attach to, and the worker that feeds it.

Streams are reference-counted. The worker encodes only the (camera, overlay) combinations that
have at least one viewer, and encodes each once however many viewers share it: N browser tabs
cost one encode, not N. With no viewers, no JPEG is ever produced.

The worker reads the latest analysed frame from the results store and never touches the
detector or capture threads, so a slow or hung browser cannot stall detection (01 §8).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from vigil.core.clock import Clock
from vigil.core.protocols.logger import LoggerLike
from vigil.core.protocols.transport import StreamKey
from vigil.core.units import NS_PER_S, ns_to_ms
from vigil.domain.frame import FrameMeta
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.annotate import RenderError, overlay_from_key, render_jpeg
from vigil.pipeline.results import LatestDetections

POLL_S = 0.02  # how often the worker looks for a new frame; the cap is `_due`, not this


@dataclass(frozen=True, slots=True)
class PublishedFrame:
    seq: int  # strictly increasing per stream
    jpeg: bytes
    meta: FrameMeta


class StreamHub:
    """Implements VideoTransport for MJPEG."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._viewers: dict[StreamKey, int] = {}
        self._latest: dict[StreamKey, PublishedFrame] = {}
        self._closed = False

    # ---- VideoTransport
    def publish(self, key: StreamKey, jpeg: bytes, meta: FrameMeta) -> None:
        with self._cond:
            previous = self._latest.get(key)
            seq = previous.seq + 1 if previous is not None else 1
            self._latest[key] = PublishedFrame(seq, jpeg, meta)
            self._cond.notify_all()

    def viewer_count(self, key: StreamKey) -> int:
        with self._cond:
            return self._viewers.get(key, 0)

    def active_keys(self) -> tuple[StreamKey, ...]:
        with self._cond:
            return tuple(k for k, n in self._viewers.items() if n > 0)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    # ---- viewers
    @contextmanager
    def subscribe(self, key: StreamKey) -> Iterator[StreamKey]:
        """Hold a viewer slot for the duration of the block."""
        with self._cond:
            self._viewers[key] = self._viewers.get(key, 0) + 1
        try:
            yield key
        finally:
            with self._cond:
                remaining = self._viewers.get(key, 1) - 1
                if remaining <= 0:
                    self._viewers.pop(key, None)  # stop encoding for this stream
                else:
                    self._viewers[key] = remaining

    def latest(self, key: StreamKey) -> PublishedFrame | None:
        with self._cond:
            return self._latest.get(key)

    def wait_next(self, key: StreamKey, after_seq: int, timeout_s: float) -> PublishedFrame | None:
        """The next frame newer than `after_seq`, or None on timeout or close."""
        with self._cond:
            self._cond.wait_for(
                lambda: (
                    self._closed or ((p := self._latest.get(key)) is not None and p.seq > after_seq)
                ),
                timeout_s,
            )
            if self._closed:
                return None
            published = self._latest.get(key)
            return published if published is not None and published.seq > after_seq else None


class StreamWorker:
    def __init__(
        self,
        *,
        hub: StreamHub,
        results: LatestDetections,
        clock: Clock,
        metrics: MetricsRegistry,
        logger: LoggerLike,
        fps: float,
        jpeg_quality: int,
    ) -> None:
        self._hub = hub
        self._results = results
        self._clock = clock
        self._metrics = metrics
        self._log = logger.bind(stage="stream")
        self._interval_s = 1.0 / fps
        self._interval_ns = round(NS_PER_S / fps)
        self._due: dict[StreamKey, int] = {}
        self._quality = jpeg_quality
        self._encoded_version: dict[StreamKey, int] = {}

    def step(self) -> int:
        """Encode every watched stream whose camera has a newer analysed frame. Returns count."""
        encoded = 0
        for key in self._hub.active_keys():
            version = self._results.version(key.camera_id)
            if version == self._encoded_version.get(key):
                continue  # nothing new for this stream
            start = self._clock.monotonic_ns()
            if start < self._due.get(key, 0):
                continue  # at the configured rate already; the newer frame waits for its slot
            analysed = self._results.latest(key.camera_id)
            if analysed is None:
                continue
            age_ms = ns_to_ms(start - analysed.completed_monotonic_ns)
            try:
                jpeg = render_jpeg(
                    analysed,
                    overlay_from_key(key.overlay),
                    jpeg_quality=self._quality,
                    age_ms=age_ms,
                )
            except RenderError as exc:
                self._metrics.counter("vigil_stream_errors_total", camera=key.camera_id).inc()
                self._log.warning("render failed", camera_id=key.camera_id, error=exc.message)
                continue
            done = self._clock.monotonic_ns()
            self._encoded_version[key] = version
            self._schedule_next(key, start)
            self._hub.publish(key, jpeg, analysed.frame.meta)
            self._metrics.histogram("vigil_stream_encode_ms").observe(ns_to_ms(done - start))
            if analysed.frame.meta.source_pts_ms is None:  # live: capture clock == this clock
                self._metrics.histogram("vigil_end_to_end_ms", camera=key.camera_id).observe(
                    ns_to_ms(done - analysed.frame.meta.t_monotonic_ns)
                )
            encoded += 1
        return encoded

    def _schedule_next(self, key: StreamKey, now_ns: int) -> None:
        """The next slot is one interval after the *previous slot*, not after this encode.

        Pacing from "now" aliased against the analysis cadence: with the stream at 10 fps and
        a camera analysed at 10 fps, a tick that landed just before a frame arrived found
        nothing, and the next tick found the frame a phase late. It was measured (D0, finding
        F8) as 8.1 to 8.9 fps delivered against 10 configured. Keeping the slot grid steady
        and polling several times per slot publishes every frame the cap allows and still
        never exceeds it. Credit is limited to one interval, so a stream that sat idle does
        not burst when a frame finally arrives.
        """
        nxt = self._due.get(key, now_ns) + self._interval_ns
        self._due[key] = now_ns if now_ns - nxt > self._interval_ns else nxt

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        poll_s = min(POLL_S, self._interval_s / 4)
        while not stop.is_set():
            beat()
            self.step()
            stop.wait(poll_s)
