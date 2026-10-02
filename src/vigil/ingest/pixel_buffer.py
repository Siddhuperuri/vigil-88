"""NumPy-backed PixelBuffer. The only place the domain's opaque pixel handle meets an array.

Imports numpy at module level, so it is imported only by concrete sources (the `capture`
extra), never by the package `__init__`.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

_COLOR_NDIM = 3


class NumpyPixelBuffer:
    """Read-only (H, W, C) uint8 image. Takes ownership of the array and freezes it."""

    __slots__ = ("_array",)

    def __init__(self, array: npt.NDArray[np.uint8]) -> None:
        if array.dtype != np.uint8:
            raise ValueError(f"expected uint8 pixels, got {array.dtype}")
        if array.ndim != _COLOR_NDIM or array.shape[2] < 1:
            raise ValueError(f"expected (H, W, C) pixels, got shape {array.shape}")
        if array.shape[0] < 1 or array.shape[1] < 1:
            raise ValueError("pixel buffer must be non-empty")
        array.setflags(write=False)
        self._array = array

    @property
    def width_px(self) -> int:
        return int(self._array.shape[1])

    @property
    def height_px(self) -> int:
        return int(self._array.shape[0])

    @property
    def channels(self) -> int:
        return int(self._array.shape[2])

    @property
    def nbytes(self) -> int:
        return int(self._array.nbytes)

    def tobytes(self) -> bytes:
        return self._array.tobytes()

    def as_ndarray(self) -> npt.NDArray[np.uint8]:
        """A read-only view. Callers that need to modify pixels must copy."""
        return self._array
