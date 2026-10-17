"""The scene-stability signal (04 §3.3, 06 §6 validator 2).

Lights switching on, auto-exposure hunting and a bumped camera are, in practice, the largest
single source of *correlated* false positives: every module fires at once, on every subject, for
the same wrong reason. One cheap whole-frame measurement suppresses the lot.

The measurement is the **median** absolute change in per-cell mean luminance between consecutive
analysed frames, over a coarse grid (16x12 by default), expressed as a fraction of full scale.
The median is the load-bearing choice: a person walking through the frame moves a handful of
cells, so the median barely moves; lights coming on move every cell and a bumped camera moves
most of them. A mean would be dragged around by the person, which is the thing we must not
suppress.

`None` means "not measured" rather than "stable" — on the first frame of a stream, after a size
change, or with no array backend installed. The analyzer reports that as a degraded fact and the
validator declines to treat it as instability (05 §1 rule 5: no sentinel values).

numpy and OpenCV are imported at module level, so this module is imported only where the
`capture` extra is installed; `pipeline/` builds the probe inside a try/except and passes `None`
when it cannot.
"""

from __future__ import annotations

import cv2
import numpy as np
import numpy.typing as npt

from vigil.core.protocols.pixels import PixelBuffer

FULL_SCALE = 255.0
_GRAY_CHANNELS = 1
_BGR_CHANNELS = 3


class LuminanceGridProbe:
    """One per camera. Holds the previous frame's grid, which is a few hundred floats."""

    __slots__ = ("_columns", "_full_change_ratio", "_previous", "_rows", "_shape")

    def __init__(self, *, columns: int, rows: int, full_change_ratio: float) -> None:
        self._columns = columns
        self._rows = rows
        self._full_change_ratio = full_change_ratio
        self._previous: npt.NDArray[np.float32] | None = None
        self._shape: tuple[int, int, int] | None = None

    def reset(self) -> None:
        self._previous = None
        self._shape = None

    def measure(self, pixels: PixelBuffer) -> float | None:
        """Stability in [0, 1] against the previous frame, or None when not measurable."""
        grid = self._grid(pixels)
        if grid is None:
            self.reset()
            return None
        previous, self._previous = self._previous, grid
        if previous is None or previous.shape != grid.shape:
            return None
        median_change = float(np.median(np.abs(grid - previous))) / FULL_SCALE
        return 1.0 - min(median_change / self._full_change_ratio, 1.0)

    def _grid(self, pixels: PixelBuffer) -> npt.NDArray[np.float32] | None:
        shape = (pixels.height_px, pixels.width_px, pixels.channels)
        if shape[2] not in (_GRAY_CHANNELS, _BGR_CHANNELS):
            return None
        if self._shape is not None and shape != self._shape:
            self._previous = None  # a resolution change makes the stored grid meaningless
        self._shape = shape
        flat = np.frombuffer(pixels.buffer(), dtype=np.uint8)
        if flat.size != shape[0] * shape[1] * shape[2]:
            return None
        image = flat.reshape(shape)
        gray = (
            image[:, :, 0]
            if shape[2] == _GRAY_CHANNELS
            else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        )
        # INTER_AREA is exactly a box-filter downsample, so each output cell is the mean
        # luminance of its block: the grid we want, computed in one C loop.
        reduced = cv2.resize(gray, (self._columns, self._rows), interpolation=cv2.INTER_AREA)
        return np.asarray(reduced, dtype=np.float32)
