"""Trying a camera and listing devices: bounded, honest, and never leaking a secret."""

from __future__ import annotations

import sys
import threading
import types

import numpy as np
import pytest

from vigil.core.errors import SourceError
from vigil.core.protocols.source import RawFrame, SourceInfo, SourceTiming
from vigil.domain.camera import SourceSpec
from vigil.domain.enums import SourceKind
from vigil.ingest.pixel_buffer import NumpyPixelBuffer
from vigil.ingest.probe import list_devices, probe_source

SPEC = SourceSpec(SourceKind.SYNTHETIC, "synthetic", {})


class Fake:
    def __init__(
        self,
        *,
        frame: bool = True,
        open_error: Exception | None = None,
        block: threading.Event | None = None,
    ):
        self._frame, self._open_error, self._block = frame, open_error, block
        self.closed = False
        self.spec = SPEC
        self.timing = SourceTiming.LIVE
        self.is_open = False

    def open(self) -> SourceInfo:
        if self._block is not None:
            self._block.wait(10)
        if self._open_error is not None:
            raise self._open_error
        return SourceInfo(64, 48, 15.0, "fake", "a detail")

    def read(self) -> RawFrame | None:
        if not self._frame:
            return None
        return RawFrame(NumpyPixelBuffer(np.zeros((48, 64, 3), np.uint8)))

    def close(self) -> None:
        self.closed = True


def test_a_source_that_opens_and_reads_reports_what_it_granted_and_is_closed() -> None:
    fake = Fake()
    result = probe_source(SPEC, build=lambda _s: fake)
    assert result.ok and (result.width_px, result.height_px, result.fps) == (64, 48, 15.0)
    assert result.backend == "fake" and "64x48" in result.message and fake.closed


def test_a_source_that_opens_but_sends_no_frame_is_a_failure_with_that_reason() -> None:
    fake = Fake(frame=False)
    result = probe_source(SPEC, build=lambda _s: fake)
    assert not result.ok and "no frame" in result.message and fake.closed


def test_a_source_error_message_comes_through_and_the_source_is_still_closed() -> None:
    fake = Fake(
        open_error=SourceError("could not connect to rtsp://cam.local/live", retryable=True)
    )
    result = probe_source(SPEC, build=lambda _s: fake)
    assert not result.ok and result.message == "could not connect to rtsp://cam.local/live"
    assert fake.closed


def test_an_unexpected_exception_is_reported_by_type_only_because_its_text_may_hold_a_url() -> None:
    fake = Fake(open_error=RuntimeError("failed opening rtsp://admin:hunter2@cam.local/x"))
    result = probe_source(SPEC, build=lambda _s: fake)
    assert not result.ok and "RuntimeError" in result.message and "hunter2" not in result.message


def test_a_probe_that_hangs_is_abandoned_at_its_deadline_and_says_so() -> None:
    gate = threading.Event()
    try:
        result = probe_source(SPEC, timeout_s=0.2, build=lambda _s: Fake(block=gate))
    finally:
        gate.set()  # let the abandoned thread finish
    assert result.timed_out and not result.ok and "abandoned" in result.message


def test_a_build_that_raises_is_a_failure_not_a_crash() -> None:
    def build(_s: SourceSpec) -> Fake:
        raise SourceError("the device is not there", retryable=False)

    assert probe_source(SPEC, build=build).message == "the device is not there"


def test_devices_come_from_the_enumerator_deduplicated_and_sorted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cam:
        def __init__(self, index: int, name: str) -> None:
            self.index, self.name = index, name

    module = types.ModuleType("cv2_enumerate_cameras")
    module.enumerate_cameras = lambda api: [Cam(1, "B cam"), Cam(0, "A cam")]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cv2_enumerate_cameras", module)
    found = list_devices()
    assert found.available and found.reason is None
    assert [(d.index, d.name) for d in found.devices] == [
        (0, "A cam"),
        (1, "B cam"),
    ]  # one each, not two


def test_without_the_enumerator_the_list_is_unavailable_with_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "cv2_enumerate_cameras", None)
    found = list_devices()
    assert not found.available and found.devices == () and "capture extra" in (found.reason or "")
