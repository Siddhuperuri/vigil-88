"""Letterbox preprocessing with an invertible transform (04 §5.3).

The transform (scale, resized size) travels with each frame and is recomputed per frame, never
cached: a resolution change mid-stream would silently invalidate a cached one. Returned boxes
are mapped back to SOURCE-frame pixels by `to_source_xyxy`; model-space coordinates never leave
the vision engine (04 §3.1).

YOLOX convention: BGR channel order, raw 0-255 floats (no mean/std), scaled by a single factor
and padded bottom/right with 114.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import numpy.typing as npt

from vigil.core.errors import InferenceError

PAD_VALUE = 114
_CHANNELS = 3

F32 = npt.NDArray[np.float32]
U8 = npt.NDArray[np.uint8]


@dataclass(frozen=True, slots=True)
class LetterboxTransform:
    src_width_px: int
    src_height_px: int
    size_px: int
    scale: float
    resized_width_px: int
    resized_height_px: int

    def to_source_xyxy(self, boxes: F32) -> F32:
        """Model-space (x1, y1, x2, y2) -> source-frame pixels, clipped to the frame."""
        out = (boxes / self.scale).astype(np.float32, copy=False)
        out[:, [0, 2]] = np.clip(out[:, [0, 2]], 0.0, float(self.src_width_px))
        out[:, [1, 3]] = np.clip(out[:, [1, 3]], 0.0, float(self.src_height_px))
        return out


def compute_letterbox(src_width_px: int, src_height_px: int, size_px: int) -> LetterboxTransform:
    if min(src_width_px, src_height_px, size_px) < 1:
        raise InferenceError(
            f"cannot letterbox a {src_width_px}x{src_height_px} frame to {size_px}px"
        )
    scale = min(size_px / src_height_px, size_px / src_width_px)
    return LetterboxTransform(
        src_width_px=src_width_px,
        src_height_px=src_height_px,
        size_px=size_px,
        scale=scale,
        resized_width_px=max(1, int(src_width_px * scale)),
        resized_height_px=max(1, int(src_height_px * scale)),
    )


def letterbox_bgr(image: U8, size_px: int) -> tuple[F32, LetterboxTransform]:
    """(H, W, 3) uint8 BGR -> (3, size, size) float32 plus the transform that inverts it."""
    if image.ndim != _CHANNELS or image.shape[2] != _CHANNELS or image.dtype != np.uint8:
        raise InferenceError(
            f"expected (H, W, 3) uint8 pixels, got shape {image.shape} dtype {image.dtype}"
        )
    height, width = int(image.shape[0]), int(image.shape[1])
    t = compute_letterbox(width, height, size_px)
    resized = cv2.resize(
        image, (t.resized_width_px, t.resized_height_px), interpolation=cv2.INTER_LINEAR
    )
    canvas = np.full((size_px, size_px, _CHANNELS), PAD_VALUE, dtype=np.uint8)
    canvas[: t.resized_height_px, : t.resized_width_px] = resized
    chw = np.ascontiguousarray(canvas.transpose(2, 0, 1), dtype=np.float32)
    return chw, t
