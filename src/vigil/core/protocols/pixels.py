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

    def buffer(self) -> memoryview:
        """A read-only, zero-copy view of the (H, W, C) uint8 pixels, row-major.

        This is how an engine reads pixels without importing the engine that produced them
        and without copying: `numpy.frombuffer(buf.buffer(), numpy.uint8)` shares memory.
        """
        ...
