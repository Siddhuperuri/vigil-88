"""Encoding evidence pixels: snapshots and clips (05 §6, 08 §4).

This lives in L5 rather than in `response/` because it needs cv2 and numpy, and because the
annotated snapshot is drawn by the same code that draws the live stream -- one renderer, so an
operator reviewing an incident sees the boxes they saw on the wall.

The clip muxer is honest about what it can do. OpenCV's `VideoWriter` depends on the codecs the
local build was compiled with, so availability is **probed once at runtime** rather than
assumed: if no writer can be opened, `mux_clip` reports why and the incident's evidence is marked
partial. There is no silent fallback to a format nobody asked for, and no zero-byte placeholder.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vigil.core.optional import optional_import
from vigil.core.units import NS_PER_S
from vigil.ingest.buffers import EncodedFrame

if TYPE_CHECKING:  # the real modules, for type checking only
    import cv2
    import numpy as np
else:  # at run time they are optional: see `core/optional.py`
    cv2, _ = optional_import("cv2")
    np, _ = optional_import("numpy")

# mp4v is the most widely available MPEG-4 encoder in OpenCV's own wheels. The codec is recorded
# in the manifest through the file's media type, so a reviewer knows what they have.
FOURCC = "mp4v"
MIN_CLIP_FRAMES = 2
DEFAULT_CLIP_FPS = 10.0


@dataclass(frozen=True, slots=True)
class ClipResult:
    written: bool
    frames: int
    reason: str | None = None
    span_ms: tuple[float, float] | None = None


def decode(frame: EncodedFrame) -> np.ndarray | None:
    """JPEG bytes back to pixels. None when the bytes are not a decodable image."""
    buffer = np.frombuffer(frame.jpeg, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    return None if image is None else image


def clip_fps(frames: Sequence[EncodedFrame], *, fallback: float = DEFAULT_CLIP_FPS) -> float:
    """The rate the frames were actually sampled at, so playback runs at real speed.

    Measured from the frames rather than taken from config: the sampler may have been starved,
    and a clip played at a rate it was not captured at misrepresents how fast things happened.
    """
    if len(frames) < MIN_CLIP_FRAMES:
        return fallback
    span_s = (frames[-1].meta.t_monotonic_ns - frames[0].meta.t_monotonic_ns) / NS_PER_S
    if span_s <= 0.0:
        return fallback
    return max(1.0, min((len(frames) - 1) / span_s, 60.0))


def mux_clip(frames: Sequence[EncodedFrame], target: Path, *, anchor_ns: int) -> ClipResult:
    """Write `frames` as an MP4 at `target`. Reports failure rather than raising.

    `anchor_ns` is the instant spans are measured from (the incident's first observation), so
    `span_ms` on the evidence row says where in the incident's timeline the clip sits.
    """
    if len(frames) < MIN_CLIP_FRAMES:
        return ClipResult(False, len(frames), "not enough buffered frames to make a clip")
    first = decode(frames[0])
    if first is None:
        return ClipResult(False, 0, "the buffered frames could not be decoded")
    height, width = first.shape[:2]
    # OpenCV 5 moved this onto the class; 4.x only had the module-level alias.
    fourcc = cv2.VideoWriter.fourcc(*FOURCC)
    writer = cv2.VideoWriter(str(target), fourcc, clip_fps(frames), (width, height))
    if not writer.isOpened():
        writer.release()
        target.unlink(missing_ok=True)
        return ClipResult(
            False,
            0,
            f"this OpenCV build cannot open a {FOURCC} writer, so no clip was produced",
        )
    written = 0
    try:
        for frame in frames:
            image = first if written == 0 else decode(frame)
            if image is None or image.shape[:2] != (height, width):
                continue  # a size change mid-buffer: skip rather than stretch the footage
            writer.write(image)
            written += 1
    finally:
        writer.release()
    if written < MIN_CLIP_FRAMES:
        target.unlink(missing_ok=True)
        return ClipResult(False, written, "too few frames survived decoding to make a clip")
    span = (
        (frames[0].meta.t_monotonic_ns - anchor_ns) / 1_000_000,
        (frames[-1].meta.t_monotonic_ns - anchor_ns) / 1_000_000,
    )
    return ClipResult(True, written, None, span)
