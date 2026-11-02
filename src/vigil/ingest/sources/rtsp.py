"""RTSP capture over OpenCV's FFmpeg backend (07 §7).

What is and is not handled, stated so nothing is assumed:

  * **TCP transport.** UDP loses packets, and a security camera that drops a frame in the middle of
    an event is worse than one that is a little late. OpenCV reads the transport from the process
    environment, so it is set once here; it applies to every FFmpeg capture in this process.
  * **A real read timeout.** The capture is opened with OpenCV's open and read timeouts, so a camera
    that goes silent raises instead of blocking a thread for ever (the failure the webcam path can
    only detect, ADR 0011). A timed-out read is retryable and the reconnect machinery takes over.
  * **Credentials only from the secret store**, resolved at open, never held in a field and never in
    a message: every message uses `redact_url`.

**Not verified against any real camera or server.** The tests drive this through a fake capture,
which proves the handling and not the protocol. Vendors differ (authentication schemes, keyframe
intervals, codecs) and none of that has been exercised.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Protocol

import cv2
import numpy as np

from vigil.core.errors import SourceError, TransientSourceError
from vigil.core.protocols.source import RawFrame, SourceInfo, SourceTiming
from vigil.domain.camera import SourceSpec
from vigil.ingest.credentials import (
    Credentials,
    redact_url,
    resolve_credentials,
    with_credentials,
)
from vigil.ingest.pixel_buffer import NumpyPixelBuffer

OPEN_TIMEOUT_MS = 8_000
READ_TIMEOUT_MS = 6_000
_COLOR_NDIM = 3
_GRAY_NDIM = 2

# Process-wide, and read by FFmpeg when a capture opens. `setdefault`: a person who set their own
# options for a reason keeps them.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


class RtspCaptureLike(Protocol):
    def isOpened(self) -> bool: ...  # mirrors the OpenCV method name
    def read(self) -> tuple[bool, object]: ...
    def get(self, prop: int) -> float: ...
    def release(self) -> None: ...


RtspCaptureFactory = Callable[[str, list[int]], RtspCaptureLike]


def _default_factory(url: str, params: list[int]) -> RtspCaptureLike:
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)


class RtspSource:
    def __init__(
        self,
        spec: SourceSpec,
        *,
        capture_factory: RtspCaptureFactory | None = None,
        env: Mapping[str, str] | None = None,
        keyring_get: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._spec = spec
        self._factory = capture_factory or _default_factory
        self._env = env
        self._keyring_get = keyring_get
        self._cap: RtspCaptureLike | None = None
        self._pending: RawFrame | None = None

    @property
    def spec(self) -> SourceSpec:
        return self._spec

    @property
    def timing(self) -> SourceTiming:
        return SourceTiming.LIVE

    @property
    def is_open(self) -> bool:
        return self._cap is not None

    def _credentials(self) -> Credentials | None:
        if self._keyring_get is None:
            return resolve_credentials(self._spec.options, env=self._env)
        return resolve_credentials(self._spec.options, env=self._env, get=self._keyring_get)

    def open(self) -> SourceInfo:
        self.close()
        safe = redact_url(self._spec.locator)
        creds = self._credentials()  # may raise SourceError naming a *name*, never a value
        url = with_credentials(self._spec.locator, creds)
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
            OPEN_TIMEOUT_MS,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC,
            READ_TIMEOUT_MS,
        ]
        try:
            cap = self._factory(url, params)
        except cv2.error as exc:
            raise SourceError(
                f"OpenCV could not start {safe}: {type(exc).__name__}", retryable=True
            ) from None  # the exception text can echo the URL, credentials included
        if not cap.isOpened():
            cap.release()
            raise SourceError(
                f"could not connect to {safe}: the camera may be off, unreachable, or the "
                "credentials may be wrong",
                retryable=True,
            )
        try:
            first = self._decode(cap.read(), safe)
        except TransientSourceError as exc:
            cap.release()
            raise SourceError(str(exc), retryable=True) from None
        self._cap, self._pending = cap, first
        fps = cap.get(cv2.CAP_PROP_FPS)
        return SourceInfo(
            width_px=first.pixels.width_px,
            height_px=first.pixels.height_px,
            fps=fps if fps and fps > 0 else None,
            backend="opencv-ffmpeg",
            detail="TCP transport",
        )

    @staticmethod
    def _decode(result: tuple[bool, object], safe: str) -> RawFrame:
        ok, frame = result
        if not ok or not isinstance(frame, np.ndarray) or frame.size == 0:
            raise TransientSourceError(f"no frame from {safe} (timed out or the stream ended)")
        if frame.ndim == _GRAY_NDIM:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        if frame.ndim != _COLOR_NDIM:
            raise TransientSourceError(f"unexpected frame shape {frame.shape}")
        return RawFrame(NumpyPixelBuffer(np.ascontiguousarray(frame, dtype=np.uint8)))

    def read(self) -> RawFrame | None:
        if self._cap is None:
            raise SourceError("the RTSP source is not open")
        if self._pending is not None:
            raw, self._pending = self._pending, None
            return raw
        try:
            return self._decode(self._cap.read(), redact_url(self._spec.locator))
        except cv2.error as exc:
            raise TransientSourceError(
                f"OpenCV error while reading: {type(exc).__name__}"
            ) from None

    def close(self) -> None:
        cap, self._cap, self._pending = self._cap, None, None
        if cap is not None:
            cap.release()
