"""Deterministic test-pattern source.

A real, honest source of known pixels: a gradient with a moving square, fully determined by
(seed, frame index). It exists so the pipeline, selftest, replay-determinism tests and the
benchmark have frames without hardware. It is *not* a simulation of a scene: its content is
synthetic, so detectors find nothing in it and post-processing cost is that of an empty scene.

Two modes:
  * default (PACED): frames are produced as fast as they are consumed, never skipped, with
    media timestamps. This is the reproducible replay mode.
  * `realtime` (LIVE): frames are produced at the configured fps on the real clock, with real
    capture timestamps and latest-frame semantics, like a camera. Used for benchmarking, where
    resolution, frame rate and camera count must be controllable.
"""

from __future__ import annotations

import threading

import numpy as np

from vigil.core.clock import Clock, SystemClock
from vigil.core.errors import SourceError
from vigil.core.protocols.source import RawFrame, SourceInfo, SourceTiming
from vigil.core.rng import seeded_rng
from vigil.core.units import NS_PER_S
from vigil.domain.camera import SourceSpec
from vigil.ingest.pixel_buffer import NumpyPixelBuffer

DEFAULT_WIDTH_PX = 320
DEFAULT_HEIGHT_PX = 240
DEFAULT_FPS = 30.0
SQUARE_PX = 40
MS_PER_S = 1000.0
_MIN_DIM_PX = SQUARE_PX * 2


class SyntheticSource:
    def __init__(self, spec: SourceSpec, clock: Clock | None = None) -> None:
        self._spec = spec
        opts = spec.options
        self._clock = clock or SystemClock()
        self._width = int(opts.get("width_px", DEFAULT_WIDTH_PX))
        self._height = int(opts.get("height_px", DEFAULT_HEIGHT_PX))
        self._fps = float(opts.get("fps", DEFAULT_FPS))
        self._realtime = bool(opts.get("realtime", False))
        count = opts.get("frame_count")
        self._frame_count = int(count) if count is not None else None
        if min(self._width, self._height) < _MIN_DIM_PX or self._fps <= 0:
            raise SourceError(
                "synthetic source: dimensions too small or fps not positive", retryable=False
            )
        rng = seeded_rng(int(opts.get("seed", 0)), "synthetic")
        self._phase = rng.randrange(0, 200)
        self._colour = tuple(rng.randrange(120, 256) for _ in range(3))
        ramp = np.linspace(30, 200, self._width, dtype=np.uint8)
        base = np.broadcast_to(ramp[None, :, None], (self._height, self._width, 3))
        self._base = np.ascontiguousarray(base)
        self._period_ns = round(NS_PER_S / self._fps)
        self._index = 0
        self._start_ns = 0
        self._open = False
        self._wake = threading.Event()  # an interruptible sleep: set by close()

    @property
    def spec(self) -> SourceSpec:
        return self._spec

    @property
    def timing(self) -> SourceTiming:
        return SourceTiming.LIVE if self._realtime else SourceTiming.PACED

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> SourceInfo:
        self._index = 0
        self._start_ns = self._clock.monotonic_ns()
        self._wake.clear()
        self._open = True
        detail = "test pattern, real time" if self._realtime else "test pattern"
        return SourceInfo(self._width, self._height, self._fps, "synthetic", detail)

    def _pace(self, index: int) -> None:
        """Hold a camera-like frame rate: frame `index` is due at start + index * period."""
        due = self._start_ns + index * self._period_ns
        now = self._clock.monotonic_ns()
        if now - due > self._period_ns:  # fell behind: resume the cadence, never burst to catch up
            self._start_ns = now - index * self._period_ns
        elif due > now:
            self._wake.wait((due - now) / NS_PER_S)

    def read(self) -> RawFrame | None:
        if not self._open:
            raise SourceError("synthetic source is not open")
        if self._frame_count is not None and self._index >= self._frame_count:
            return None
        idx = self._index
        self._index += 1
        if self._realtime:
            self._pace(idx)
            if self._wake.is_set():  # close() was called while we waited
                return None
        arr = self._base.copy()
        x = (self._phase + idx * 4) % (self._width - SQUARE_PX)
        y = (self._phase + idx * 3) % (self._height - SQUARE_PX)
        arr[y : y + SQUARE_PX, x : x + SQUARE_PX] = np.array(self._colour, dtype=np.uint8)
        pts = None if self._realtime else idx * MS_PER_S / self._fps
        return RawFrame(NumpyPixelBuffer(arr), source_pts_ms=pts)

    def close(self) -> None:
        self._open = False
        self._wake.set()
