"""Real-device tests. Skipped unless run with `--webcam` (they light the camera LED)."""

from __future__ import annotations

import pytest

from vigil.domain import SourceKind, SourceSpec
from vigil.ingest.sources.webcam import WebcamSource

pytestmark = pytest.mark.webcam


def test_real_webcam_opens_reads_and_closes() -> None:
    src = WebcamSource(SourceSpec(SourceKind.WEBCAM, "webcam:0", {"device_index": 0}))
    info = src.open()
    try:
        assert info.width_px > 0 and info.height_px > 0
        for _ in range(10):
            raw = src.read()
            assert raw is not None
            assert (raw.pixels.width_px, raw.pixels.height_px) == (info.width_px, info.height_px)
    finally:
        src.close()
    assert not src.is_open
