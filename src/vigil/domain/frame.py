"""Frame and its identity (05 §4).

`(camera_id, stream_epoch, frame_index)` identifies a frame. The epoch increments on every
reconnect, so indices never collide across a gap and any state keyed by frame identity is
invalidated by a reconnect automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vigil.core.ids import validate_camera_id
from vigil.core.protocols.pixels import PixelBuffer
from vigil.domain._validate import require_non_negative, require_positive, require_utc


@dataclass(frozen=True, slots=True)
class FrameMeta:
    camera_id: str
    stream_epoch: int
    frame_index: int
    t_monotonic_ns: int
    wall_utc: datetime
    width_px: int
    height_px: int
    source_pts_ms: float | None
    is_keyframe: bool | None

    def __post_init__(self) -> None:
        validate_camera_id(self.camera_id)
        require_non_negative("stream_epoch", self.stream_epoch)
        require_non_negative("frame_index", self.frame_index)
        require_non_negative("t_monotonic_ns", self.t_monotonic_ns)
        require_positive("width_px", self.width_px)
        require_positive("height_px", self.height_px)
        require_utc("FrameMeta.wall_utc", self.wall_utc)
        if self.source_pts_ms is not None:
            require_non_negative("source_pts_ms", self.source_pts_ms)

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.camera_id, self.stream_epoch, self.frame_index)


@dataclass(frozen=True, slots=True)
class Frame:
    """Pixel buffers are never mutated after construction (01 §8 invariant 2)."""

    meta: FrameMeta
    pixels: PixelBuffer

    def __post_init__(self) -> None:
        actual = (self.pixels.width_px, self.pixels.height_px)
        if actual != (self.meta.width_px, self.meta.height_px):
            raise ValueError(
                f"pixel buffer is {self.pixels.width_px}x{self.pixels.height_px} "
                f"but meta says {self.meta.width_px}x{self.meta.height_px}"
            )
