"""Webcam source over OpenCV (07 §7).

Windows specifics handled here:
  * MSMF first, DSHOW as fallback (some devices work on only one, unpredictably);
  * request MJPG *before* resolution (many USB cameras cap at 5-10 fps in YUY2);
  * verify what was GRANTED from the first real frame, not from what was requested;
  * the first frame is read inside open() to prove the device delivers, and is returned by
    the first read() so no frame is lost.

Known limitation: `VideoCapture.read()` cannot be interrupted or given a timeout, and
releasing a capture from another thread is not safe. A hung read is therefore *detected*
(watchdog -> DEGRADED "stalled") but not forcibly cancelled. See docs/LIMITATIONS.md.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Protocol

import cv2
import numpy as np

from vigil.core.errors import SourceError, TransientSourceError
from vigil.core.protocols.source import RawFrame, SourceInfo, SourceTiming
from vigil.domain.camera import SourceSpec
from vigil.ingest.pixel_buffer import NumpyPixelBuffer

_COLOR_NDIM = 3
_IS_WINDOWS = sys.platform == "win32"  # not inlined: keeps mypy from pruning the fallback
_GRAY_NDIM = 2


class CaptureLike(Protocol):
    """The slice of cv2.VideoCapture used here; lets tests substitute a fake device."""

    def isOpened(self) -> bool: ...  # mirrors the OpenCV method name
    def read(self) -> tuple[bool, object]: ...
    def set(self, prop: int, value: float) -> bool: ...
    def get(self, prop: int) -> float: ...
    def release(self) -> None: ...


CaptureFactory = Callable[[int, int], CaptureLike]


def _default_factory(index: int, api: int) -> CaptureLike:
    return cv2.VideoCapture(index, api)


def backend_order(name: str) -> list[tuple[str, int]]:
    if name == "msmf":
        return [("msmf", cv2.CAP_MSMF)]
    if name == "dshow":
        return [("dshow", cv2.CAP_DSHOW)]
    if _IS_WINDOWS:
        return [("msmf", cv2.CAP_MSMF), ("dshow", cv2.CAP_DSHOW)]
    return [("any", cv2.CAP_ANY)]


def _fourcc(code: str) -> int:
    return int(cv2.VideoWriter.fourcc(*code))


class WebcamSource:
    def __init__(self, spec: SourceSpec, *, capture_factory: CaptureFactory | None = None) -> None:
        self._spec = spec
        opts = spec.options
        index = opts.get("device_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise SourceError("webcam source requires an integer device_index", retryable=False)
        self._index = index
        self._backend = str(opts.get("backend", "auto"))
        self._width = self._opt_int("width_px")
        self._height = self._opt_int("height_px")
        self._fps = float(opts["fps"]) if "fps" in opts else None
        fourcc = opts.get("fourcc", "MJPG")
        self._fourcc = str(fourcc) if fourcc else None
        self._factory = capture_factory or _default_factory
        self._cap: CaptureLike | None = None
        self._pending: RawFrame | None = None

    def _opt_int(self, key: str) -> int | None:
        value = self._spec.options.get(key)
        return int(value) if value is not None else None

    @property
    def spec(self) -> SourceSpec:
        return self._spec

    @property
    def timing(self) -> SourceTiming:
        return SourceTiming.LIVE

    @property
    def is_open(self) -> bool:
        return self._cap is not None

    def open(self) -> SourceInfo:
        self.close()
        tried: list[str] = []
        for name, api in backend_order(self._backend):
            tried.append(name)
            info = self._try_backend(name, api)
            if info is not None:
                return info
        raise SourceError(
            f"could not open webcam {self._index} (tried {', '.join(tried)}); it may be "
            "absent, in use by another application, or blocked by Windows camera privacy settings",
            retryable=True,
            context={"device_index": self._index},
        )

    def _try_backend(self, name: str, api: int) -> SourceInfo | None:
        try:
            cap = self._factory(self._index, api)
        except cv2.error:
            return None
        if not cap.isOpened():
            cap.release()
            return None
        try:
            if self._fourcc:
                cap.set(cv2.CAP_PROP_FOURCC, _fourcc(self._fourcc))
            if self._width:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._width))
            if self._height:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._height))
            if self._fps:
                cap.set(cv2.CAP_PROP_FPS, self._fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1.0)  # best effort; many backends ignore it
            raw = self._decode(cap.read())
        except (cv2.error, TransientSourceError):
            cap.release()
            return None
        granted_fps = cap.get(cv2.CAP_PROP_FPS)
        self._cap = cap
        self._pending = raw
        detail = f"requested {self._width}x{self._height}@{self._fps} {self._fourcc}"
        return SourceInfo(
            width_px=raw.pixels.width_px,
            height_px=raw.pixels.height_px,
            fps=granted_fps if granted_fps and granted_fps > 0 else None,
            backend=name,
            detail=detail,
        )

    @staticmethod
    def _decode(result: tuple[bool, object]) -> RawFrame:
        ok, frame = result
        if not ok or not isinstance(frame, np.ndarray) or frame.size == 0:
            raise TransientSourceError("webcam returned no frame")
        if frame.ndim == _GRAY_NDIM:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        if frame.ndim != _COLOR_NDIM:
            raise TransientSourceError(f"unexpected frame shape {frame.shape}")
        return RawFrame(NumpyPixelBuffer(np.ascontiguousarray(frame, dtype=np.uint8)))

    def read(self) -> RawFrame | None:
        if self._cap is None:
            raise SourceError("webcam is not open")
        if self._pending is not None:
            raw, self._pending = self._pending, None
            return raw
        try:
            return self._decode(self._cap.read())
        except cv2.error as exc:
            raise TransientSourceError(f"OpenCV error while reading: {exc}") from exc

    def close(self) -> None:
        cap, self._cap, self._pending = self._cap, None, None
        if cap is not None:
            cap.release()
