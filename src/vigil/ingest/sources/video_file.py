"""Video-file replay (07 §2): a PACED source.

Nothing may be skipped, so the consumer blocks and the source supplies the media timestamps that
drive the replay clock. That is what makes a replay reproducible: the same file produces the same
frames at the same media times however fast the machine is.

Looping continues the timeline rather than restarting it at zero: a replay clock that jumped back
would make every temporal decision see time run backwards.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from vigil.core.errors import SourceError, TransientSourceError
from vigil.core.protocols.source import RawFrame, SourceInfo, SourceTiming
from vigil.domain.camera import SourceSpec
from vigil.ingest.pixel_buffer import NumpyPixelBuffer

_PREFIX = "video_file:"
_COLOR_NDIM = 3
_GRAY_NDIM = 2
_DEFAULT_FPS = 25.0  # only used to space frames when a file reports no rate


class FileCaptureLike(Protocol):
    def isOpened(self) -> bool: ...  # mirrors the OpenCV method name
    def read(self) -> tuple[bool, object]: ...
    def set(self, prop: int, value: float) -> bool: ...
    def get(self, prop: int) -> float: ...
    def release(self) -> None: ...


FileCaptureFactory = Callable[[str], FileCaptureLike]


def _default_factory(path: str) -> FileCaptureLike:
    return cv2.VideoCapture(path)


class VideoFileSource:
    def __init__(self, spec: SourceSpec, *, capture_factory: FileCaptureFactory | None = None):
        self._spec = spec
        self._path = spec.locator.removeprefix(_PREFIX)
        self._loop = bool(spec.options.get("loop", False))
        self._factory = capture_factory or _default_factory
        self._cap: FileCaptureLike | None = None
        self._fps = _DEFAULT_FPS
        self._offset_ms = 0.0  # added to the media time after each loop, so time never goes back
        self._last_ms = 0.0
        self._index = 0
        self._after_seek = False
        self._loop_index = 0  # frames since the start of the file or the last loop

    @property
    def spec(self) -> SourceSpec:
        return self._spec

    @property
    def timing(self) -> SourceTiming:
        return SourceTiming.PACED

    @property
    def is_open(self) -> bool:
        return self._cap is not None

    def open(self) -> SourceInfo:
        self.close()
        name = Path(self._path).name  # a name in a message, never the folder it came from
        if not Path(self._path).is_file():
            raise SourceError(f"the video file {name!r} does not exist", retryable=False)
        try:
            cap = self._factory(self._path)
        except cv2.error as exc:
            raise SourceError(f"OpenCV could not open {name!r}: {exc}", retryable=False) from exc
        if not cap.isOpened():
            cap.release()
            raise SourceError(
                f"{name!r} could not be opened as a video: it may be damaged or use a codec "
                "this build cannot decode",
                retryable=False,
            )
        width, height = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        fps = cap.get(cv2.CAP_PROP_FPS)
        if width <= 0 or height <= 0:
            cap.release()
            raise SourceError(f"{name!r} reports no picture size", retryable=False)
        self._fps = fps if fps and fps > 0 else _DEFAULT_FPS
        self._cap, self._offset_ms, self._last_ms, self._index = cap, 0.0, 0.0, 0
        self._loop_index = 0
        return SourceInfo(
            width_px=width,
            height_px=height,
            fps=fps if fps and fps > 0 else None,
            backend="opencv-file",
            detail="looping" if self._loop else "plays once, then ends",
        )

    def read(self) -> RawFrame | None:
        if self._cap is None:
            raise SourceError("the video file is not open")
        ok, frame = self._cap.read()
        if not ok and self._loop:
            self._offset_ms = self._last_ms + 1000.0 / self._fps
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0.0)
            self._after_seek = True
            self._loop_index = 0
            ok, frame = self._cap.read()
        if not ok:
            return None  # the end of the file is a normal event, not an error
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            raise TransientSourceError("the video file produced an empty frame")
        if frame.ndim == _GRAY_NDIM:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        if frame.ndim != _COLOR_NDIM:
            raise TransientSourceError(f"unexpected frame shape {frame.shape}")
        # After a seek OpenCV reports the position of the frame *before* the seek for the first
        # read (measured: 1000 ms in a 500 ms-long file), so that frame is the start of the media.
        media_ms = 0.0 if self._after_seek else self._cap.get(cv2.CAP_PROP_POS_MSEC)
        if self._loop_index > 0 and (not media_ms or media_ms <= 0):
            # Some containers report no time at all: space the frames by the file's rate instead.
            media_ms = self._loop_index * 1000.0 / self._fps
        self._after_seek = False
        self._last_ms = self._offset_ms + media_ms
        self._index += 1
        self._loop_index += 1
        return RawFrame(
            NumpyPixelBuffer(np.ascontiguousarray(frame, dtype=np.uint8)),
            source_pts_ms=self._last_ms,
        )

    def close(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()
