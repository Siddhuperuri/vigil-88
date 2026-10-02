"""Frame sources (07 §2).

`read()` returns a RawFrame, not a Frame: a Frame is immutable and carries stream epoch,
index and capture timestamps that only the capture worker can assign, immediately after
`read()` returns (07 §4). See docs/adr/0011.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.core.protocols.pixels import PixelBuffer
    from vigil.domain.camera import SourceSpec


class SourceTiming(StrEnum):
    LIVE = "live"  # webcam, RTSP: latest-only buffering, skipping is normal and counted
    PACED = "paced"  # video file: blocking handoff, nothing may be skipped
    ONE_SHOT = "one_shot"  # image: one frame, then end of stream


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """What the source actually granted, which may differ from what was requested."""

    width_px: int
    height_px: int
    fps: float | None
    backend: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class RawFrame:
    pixels: PixelBuffer
    source_pts_ms: float | None = None
    is_keyframe: bool | None = None


class FrameSource(Protocol):
    @property
    def spec(self) -> SourceSpec: ...

    @property
    def timing(self) -> SourceTiming: ...

    @property
    def is_open(self) -> bool: ...

    def open(self) -> SourceInfo:
        """Raises SourceError."""
        ...

    def read(self) -> RawFrame | None:
        """None means end of stream, which is a normal event. Errors raise SourceError;
        a single bad read raises TransientSourceError."""
        ...

    def close(self) -> None:
        """Idempotent."""
        ...
