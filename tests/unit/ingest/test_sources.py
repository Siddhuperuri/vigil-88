from __future__ import annotations

import numpy as np
import pytest

from vigil.core.errors import CapabilityError, SourceError, TransientSourceError
from vigil.core.protocols.source import SourceTiming
from vigil.domain import SourceKind, SourceSpec
from vigil.ingest.pixel_buffer import NumpyPixelBuffer
from vigil.ingest.sources.factory import IMPLEMENTED_KINDS, build_source
from vigil.ingest.sources.synthetic import SyntheticSource
from vigil.ingest.sources.webcam import WebcamSource, backend_order

# ------------------------------------------------------------------ NumpyPixelBuffer


def test_pixel_buffer_is_read_only_and_reports_geometry() -> None:
    arr = np.zeros((48, 64, 3), dtype=np.uint8)
    buf = NumpyPixelBuffer(arr)
    assert (buf.width_px, buf.height_px, buf.channels) == (64, 48, 3)
    assert buf.nbytes == 48 * 64 * 3 and len(buf.tobytes()) == buf.nbytes
    with pytest.raises(ValueError, match="read-only"):
        buf.as_ndarray()[0, 0, 0] = 1
    with pytest.raises(ValueError, match="read-only"):
        arr[0, 0, 0] = 1  # the buffer took ownership and froze the array


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((4, 4), dtype=np.uint8),
        np.zeros((4, 4, 3), dtype=np.float32),
        np.zeros((0, 4, 3), dtype=np.uint8),
        np.zeros((4, 4, 0), dtype=np.uint8),
    ],
)
def test_pixel_buffer_rejects_unsupported_arrays(bad: np.ndarray) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        NumpyPixelBuffer(bad)


# ------------------------------------------------------------------ synthetic


def synth(**opts: object) -> SyntheticSource:
    return SyntheticSource(SourceSpec(SourceKind.SYNTHETIC, "synthetic", opts))  # type: ignore[arg-type]


def read_all(src: SyntheticSource) -> list[bytes]:
    src.open()
    out = []
    while (raw := src.read()) is not None:
        out.append(raw.pixels.tobytes())
    return out


def test_synthetic_is_paced_and_reports_what_it_grants() -> None:
    src = synth(width_px=160, height_px=120, fps=25, frame_count=3)
    info = src.open()
    assert src.timing is SourceTiming.PACED and src.is_open
    assert (info.width_px, info.height_px, info.fps) == (160, 120, 25.0)


def test_synthetic_is_deterministic_for_a_seed_and_differs_across_seeds() -> None:
    a = read_all(synth(frame_count=5, seed=1))
    assert a == read_all(synth(frame_count=5, seed=1))
    assert a != read_all(synth(frame_count=5, seed=2))
    assert len(set(a)) == 5  # the pattern moves every frame


def test_synthetic_pts_advance_at_the_configured_rate_and_eos_is_none() -> None:
    src = synth(fps=20, frame_count=3)
    src.open()
    pts = []
    while (raw := src.read()) is not None:
        pts.append(raw.source_pts_ms)
    assert pts == [0.0, 50.0, 100.0]


def test_synthetic_reopen_restarts_the_stream() -> None:
    src = synth(frame_count=2)
    assert len(read_all(src)) == 2
    assert len(read_all(src)) == 2


def test_synthetic_must_be_opened_and_validates_options() -> None:
    with pytest.raises(SourceError, match="not open"):
        synth().read()
    with pytest.raises(SourceError, match="too small"):
        synth(width_px=10)
    with pytest.raises(SourceError, match="fps"):
        synth(fps=0)


def test_synthetic_close_is_idempotent() -> None:
    src = synth()
    src.open()
    src.close()
    src.close()
    assert not src.is_open


# ------------------------------------------------------------------ factory


@pytest.mark.parametrize("kind", [SourceKind.RTSP, SourceKind.VIDEO_FILE, SourceKind.IMAGE])
def test_unimplemented_kinds_fail_loudly_with_an_explanation(kind: SourceKind) -> None:
    with pytest.raises(CapabilityError, match="not implemented"):
        build_source(SourceSpec(kind, "x", {}))


def test_implemented_kinds_build() -> None:
    assert IMPLEMENTED_KINDS == {SourceKind.WEBCAM, SourceKind.SYNTHETIC}
    assert isinstance(build_source(SourceSpec(SourceKind.SYNTHETIC, "synthetic", {})), SyntheticSource)
    web = build_source(SourceSpec(SourceKind.WEBCAM, "webcam:0", {"device_index": 0}))
    assert isinstance(web, WebcamSource) and not web.is_open  # building never touches a device


# ------------------------------------------------------------------ webcam (fake device)


class FakeCapture:
    """A cv2.VideoCapture stand-in recording how it was configured."""

    def __init__(self, *, opened: bool = True, frames: list[object] | None = None,
                 size: tuple[int, int] = (640, 480)) -> None:
        self.opened = opened
        self.frames = list(frames) if frames is not None else None
        self.size = size
        self.sets: list[tuple[int, float]] = []
        self.released = 0

    def isOpened(self) -> bool:  # noqa: N802
        return self.opened

    def read(self) -> tuple[bool, object]:
        if self.frames is not None:
            if not self.frames:
                return False, None
            item = self.frames.pop(0)
            return (False, None) if item is None else (True, item)
        w, h = self.size
        return True, np.full((h, w, 3), 7, dtype=np.uint8)

    def set(self, prop: int, value: float) -> bool:
        self.sets.append((prop, value))
        return True

    def get(self, prop: int) -> float:
        return 30.0

    def release(self) -> None:
        self.released += 1


def webcam(factory: object, **opts: object) -> WebcamSource:
    spec = SourceSpec(SourceKind.WEBCAM, "webcam:0", {"device_index": 0, **opts})  # type: ignore[arg-type]
    return WebcamSource(spec, capture_factory=factory)  # type: ignore[arg-type]


def test_webcam_reports_the_granted_resolution_not_the_requested_one() -> None:
    src = webcam(lambda i, a: FakeCapture(size=(640, 480)), width_px=1920, height_px=1080)
    info = src.open()
    assert (info.width_px, info.height_px) == (640, 480)  # camera ignored the request
    assert info.fps == 30.0 and src.is_open and src.timing is SourceTiming.LIVE


def test_webcam_requests_the_codec_before_the_resolution() -> None:
    import cv2

    cap = FakeCapture()
    webcam(lambda i, a: cap, width_px=1280, height_px=720, fps=30, fourcc="MJPG").open()
    props = [p for p, _ in cap.sets]
    assert props.index(cv2.CAP_PROP_FOURCC) < props.index(cv2.CAP_PROP_FRAME_WIDTH)
    assert props.index(cv2.CAP_PROP_FRAME_WIDTH) < props.index(cv2.CAP_PROP_FRAME_HEIGHT)


def test_webcam_falls_back_to_the_second_backend_and_releases_the_failed_one() -> None:
    import cv2

    tried: list[int] = []
    caps: list[FakeCapture] = []

    def factory(index: int, api: int) -> FakeCapture:
        tried.append(api)
        cap = FakeCapture(opened=api == cv2.CAP_DSHOW)
        caps.append(cap)
        return cap

    info = webcam(factory).open()
    if len(tried) == 2:  # Windows: msmf then dshow
        assert tried == [cv2.CAP_MSMF, cv2.CAP_DSHOW] and info.backend == "dshow"
        assert caps[0].released == 1


def test_webcam_that_opens_but_delivers_nothing_counts_as_failed() -> None:
    caps = []

    def factory(i: int, a: int) -> FakeCapture:
        caps.append(FakeCapture(frames=[]))  # opens, never delivers
        return caps[-1]

    with pytest.raises(SourceError, match="could not open webcam 0") as e:
        webcam(factory).open()
    assert e.value.retryable and all(c.released >= 1 for c in caps)


def test_webcam_that_cannot_open_gives_an_actionable_retryable_error() -> None:
    with pytest.raises(SourceError, match="in use by another application") as e:
        webcam(lambda i, a: FakeCapture(opened=False)).open()
    assert e.value.retryable


def test_first_frame_is_kept_and_returned_by_the_first_read() -> None:
    cap = FakeCapture(frames=[np.full((48, 64, 3), 1, np.uint8), np.full((48, 64, 3), 2, np.uint8)])
    src = webcam(lambda i, a: cap)
    src.open()
    first, second = src.read(), src.read()
    assert first is not None and second is not None
    assert first.pixels.tobytes()[0] == 1 and second.pixels.tobytes()[0] == 2  # nothing lost


def test_webcam_read_failure_is_a_transient_error() -> None:
    cap = FakeCapture(frames=[np.zeros((48, 64, 3), np.uint8), None])
    src = webcam(lambda i, a: cap)
    src.open()
    src.read()
    with pytest.raises(TransientSourceError):
        src.read()


def test_webcam_grayscale_frames_are_converted_to_three_channels() -> None:
    cap = FakeCapture(frames=[np.zeros((48, 64), np.uint8)])
    src = webcam(lambda i, a: cap)
    src.open()
    raw = src.read()
    assert raw is not None and raw.pixels.channels == 3


def test_webcam_read_before_open_and_idempotent_close() -> None:
    cap = FakeCapture()
    src = webcam(lambda i, a: cap)
    with pytest.raises(SourceError, match="not open"):
        src.read()
    src.open()
    src.close()
    src.close()
    assert not src.is_open and cap.released == 1


def test_reopening_releases_the_previous_device_first() -> None:
    caps: list[FakeCapture] = []

    def factory(i: int, a: int) -> FakeCapture:
        caps.append(FakeCapture())
        return caps[-1]

    src = webcam(factory)
    src.open()
    src.open()
    assert caps[0].released == 1


@pytest.mark.parametrize("bad", [-1, "0", True, 1.5])
def test_webcam_validates_device_index(bad: object) -> None:
    spec = SourceSpec(SourceKind.WEBCAM, "webcam:x", {"device_index": bad})  # type: ignore[dict-item]
    with pytest.raises(SourceError, match="device_index"):
        WebcamSource(spec)


def test_backend_order_respects_explicit_choice() -> None:
    import cv2

    assert backend_order("msmf") == [("msmf", cv2.CAP_MSMF)]
    assert backend_order("dshow") == [("dshow", cv2.CAP_DSHOW)]
    assert backend_order("auto")  # at least one backend on every platform
