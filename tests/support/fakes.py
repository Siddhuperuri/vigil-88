"""Test doubles. Nothing here ships in the product."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from vigil.core.errors import SourceError
from vigil.core.protocols.source import RawFrame, SourceInfo, SourceTiming
from vigil.domain.camera import SourceSpec
from vigil.domain.enums import SourceKind


@dataclass(frozen=True)
class StubPixels:
    """A PixelBuffer with no numpy behind it, so domain tests need no capture extra."""

    width_px: int = 64
    height_px: int = 48
    channels: int = 3

    @property
    def nbytes(self) -> int:
        return self.width_px * self.height_px * self.channels

    def tobytes(self) -> bytes:
        return bytes(self.nbytes)


def raw_frame(width: int = 64, height: int = 48, pts_ms: float | None = None) -> RawFrame:
    return RawFrame(StubPixels(width, height), source_pts_ms=pts_ms)


@dataclass
class BlockUntil:
    """A scripted read that blocks until `event` is set, then reports end of stream."""

    event: threading.Event


ReadItem = RawFrame | SourceError | BlockUntil | None


def scripted_spec(kind: SourceKind = SourceKind.SYNTHETIC) -> SourceSpec:
    return SourceSpec(kind, "scripted", {})


class ScriptedSource:
    """A FrameSource driven by explicit scripts.

    `open_results`: one entry per open() call; None = success, an exception = raise it.
    Once exhausted, open() succeeds.
    `reads`: one entry per read(); a RawFrame, an exception to raise, BlockUntil, or None
    for end of stream. Once exhausted, read() reports end of stream.
    """

    def __init__(
        self,
        reads: list[ReadItem],
        *,
        open_results: list[SourceError | None] | None = None,
        timing: SourceTiming = SourceTiming.LIVE,
        width: int = 64,
        height: int = 48,
        fps: float | None = 30.0,
        on_read: Callable[[], None] | None = None,
    ) -> None:
        self._reads = list(reads)
        self._open_results = list(open_results or [])
        self._timing = timing
        self._info = SourceInfo(width, height, fps, "scripted", None)
        self._on_read = on_read
        self._open = False
        self.open_calls = 0
        self.close_calls = 0
        self.read_calls = 0

    @property
    def spec(self) -> SourceSpec:
        return scripted_spec()

    @property
    def timing(self) -> SourceTiming:
        return self._timing

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> SourceInfo:
        self.open_calls += 1
        if self._open_results:
            outcome = self._open_results.pop(0)
            if outcome is not None:
                raise outcome
        self._open = True
        return self._info

    def read(self) -> RawFrame | None:
        self.read_calls += 1
        if self._on_read is not None:
            self._on_read()
        if not self._reads:
            return None
        item = self._reads.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, BlockUntil):
            item.event.wait(30)
            return None
        return item

    def close(self) -> None:
        self.close_calls += 1
        self._open = False
