"""Draws the application icon with the standard library only: a lens ring on a dark square.

    python scripts/make_icon.py desktop/installer/icon.png

No image editor or font is involved, so the file can be regenerated and reviewed as code.
"""

from __future__ import annotations

import math
import struct
import sys
import zlib

SIZE = 256
BACKGROUND = (10, 11, 13)
RING = (45, 212, 191)
CORE = (230, 233, 239)


def pixel(x: int, y: int) -> tuple[int, int, int, int]:
    cx = cy = (SIZE - 1) / 2
    d = math.hypot(x - cx, y - cy)
    corner = max(abs(x - cx), abs(y - cy)) > SIZE / 2 - 18 and math.hypot(
        max(abs(x - cx) - (SIZE / 2 - 34), 0), max(abs(y - cy) - (SIZE / 2 - 34), 0)
    ) > 16
    if corner:
        return (0, 0, 0, 0)  # transparent rounded corners
    if 78 <= d <= 104:
        return (*RING, 255)
    if d <= 30:
        return (*CORE, 255)
    return (*BACKGROUND, 255)


def png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(
        b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(SIZE)) for y in range(SIZE)
    )
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    with open(sys.argv[1], "wb") as handle:
        handle.write(png())
