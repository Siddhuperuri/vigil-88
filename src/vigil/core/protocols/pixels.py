"""Opaque pixel storage.

The domain holds this protocol rather than a numpy array so that domain/ stays importable
in a bare interpreter and array semantics (views, mutation, dtype) never become domain
semantics (02 §5).
"""

from __future__ import annotations

from typing import Protocol


class PixelBuffer(Protocol):
    @property
    def width_px(self) -> int: ...

    @property
    def height_px(self) -> int: ...

    @property
    def channels(self) -> int: ...

    @property
    def nbytes(self) -> int: ...

    def tobytes(self) -> bytes: ...
