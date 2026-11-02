"""Video-file replay, RTSP handling and camera credentials.

RTSP is driven through a fake capture. That proves the handling (timeouts, redaction, retry
classification) and says nothing about any real camera or server.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

from vigil.core.errors import SourceError, TransientSourceError
from vigil.core.protocols.source import SourceTiming
from vigil.domain.camera import SourceSpec
from vigil.domain.enums import SourceKind
from vigil.ingest import credentials as creds
from vigil.ingest.sources.rtsp import OPEN_TIMEOUT_MS, READ_TIMEOUT_MS, RtspSource
from vigil.ingest.sources.video_file import VideoFileSource

SECRET = "p@ss:w/ord#1"  # every special character a URL treats as syntax


def write_video(
    path: Path, frames: int = 12, size: tuple[int, int] = (64, 48), fps: float = 10.0
) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), fps, size)
    assert writer.isOpened(), "this OpenCV build cannot write mp4v"
    for i in range(frames):
        frame = np.full((size[1], size[0], 3), (i * 15) % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()


def file_spec(path: Path, *, loop: bool = False) -> SourceSpec:
    return SourceSpec(SourceKind.VIDEO_FILE, f"video_file:{path}", {"loop": loop})


# ------------------------------------------------------------------ video file


def test_a_video_file_is_a_paced_source_so_nothing_may_be_skipped(tmp_path: Path) -> None:
    write_video(tmp_path / "a.mp4")
    assert VideoFileSource(file_spec(tmp_path / "a.mp4")).timing is SourceTiming.PACED


def test_it_reports_what_the_file_really_is_and_plays_every_frame_once(tmp_path: Path) -> None:
    path = tmp_path / "a.mp4"
    write_video(path, frames=12, size=(64, 48), fps=10.0)
    source = VideoFileSource(file_spec(path))
    info = source.open()
    assert (info.width_px, info.height_px) == (64, 48) and info.fps == pytest.approx(10.0, abs=0.5)
    pts = []
    while (frame := source.read()) is not None:
        assert frame.pixels.width_px == 64
        pts.append(frame.source_pts_ms)
    assert len(pts) == 12
    assert all(p is not None for p in pts) and pts == sorted(pts)  # media time only moves forward
    source.close()


def test_the_end_of_the_file_is_a_normal_event_and_reading_after_close_is_an_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a.mp4"
    write_video(path, frames=3)
    source = VideoFileSource(file_spec(path))
    source.open()
    assert [source.read() is not None for _ in range(3)] == [True] * 3
    assert source.read() is None
    source.close()
    with pytest.raises(SourceError):
        source.read()


def test_looping_continues_the_timeline_instead_of_restarting_it_at_zero(tmp_path: Path) -> None:
    """A replay clock that jumped back would make every temporal decision see time run backwards."""
    path = tmp_path / "a.mp4"
    write_video(path, frames=5)
    source = VideoFileSource(file_spec(path, loop=True))
    source.open()
    times = [source.read().source_pts_ms for _ in range(13)]  # type: ignore[union-attr]
    assert all(t is not None for t in times)
    assert times == sorted(times) and len(set(times)) == 13  # across two loops, strictly forward
    source.close()


def test_a_missing_file_is_a_permanent_error_that_names_the_file_not_the_folder(
    tmp_path: Path,
) -> None:
    source = VideoFileSource(file_spec(tmp_path / "nope.mp4"))
    with pytest.raises(SourceError) as info:
        source.open()
    assert info.value.retryable is False
    assert "nope.mp4" in str(info.value) and str(tmp_path) not in str(info.value)


def test_a_file_that_is_not_a_video_is_refused_with_a_reason(tmp_path: Path) -> None:
    bad = tmp_path / "notes.mp4"
    bad.write_text("this is not a video", encoding="utf-8")
    with pytest.raises(SourceError) as info:
        VideoFileSource(file_spec(bad)).open()
    assert info.value.retryable is False and "could not be opened as a video" in str(info.value)


class FakeCapture:
    def __init__(
        self, frames: list[object], *, opened: bool = True, size: tuple[int, int] = (8, 6)
    ) -> None:
        self._frames, self._opened, self._size = list(frames), opened, size
        self.released = False
        self.positions: list[float] = []

    def isOpened(self) -> bool:
        return self._opened

    def read(self) -> tuple[bool, object]:
        return (True, self._frames.pop(0)) if self._frames else (False, None)

    def set(self, prop: int, value: float) -> bool:
        self.positions.append(value)
        return True

    def get(self, prop: int) -> float:
        return {
            cv2.CAP_PROP_FRAME_WIDTH: float(self._size[0]),
            cv2.CAP_PROP_FRAME_HEIGHT: float(self._size[1]),
            cv2.CAP_PROP_FPS: 25.0,
            cv2.CAP_PROP_POS_MSEC: 0.0,  # a container that reports no time
        }.get(prop, 0.0)

    def release(self) -> None:
        self.released = True


def test_a_container_with_no_timestamps_gets_frames_spaced_by_the_rate(tmp_path: Path) -> None:
    path = tmp_path / "x.mp4"
    path.write_bytes(b"x")
    cap = FakeCapture([np.zeros((6, 8, 3), np.uint8) for _ in range(3)])
    source = VideoFileSource(file_spec(path), capture_factory=lambda _p: cap)
    source.open()
    assert [source.read().source_pts_ms for _ in range(3)] == [0.0, 40.0, 80.0]  # type: ignore[union-attr]


def test_grey_and_empty_frames_are_handled(tmp_path: Path) -> None:
    path = tmp_path / "x.mp4"
    path.write_bytes(b"x")
    cap = FakeCapture([np.zeros((6, 8), np.uint8), np.zeros((0,), np.uint8)])
    source = VideoFileSource(file_spec(path), capture_factory=lambda _p: cap)
    source.open()
    assert source.read() is not None  # grey is widened to colour
    with pytest.raises(TransientSourceError):
        source.read()


def test_a_capture_that_will_not_open_or_reports_no_size_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.mp4"
    path.write_bytes(b"x")
    closed = FakeCapture([], opened=False)
    with pytest.raises(SourceError):
        VideoFileSource(file_spec(path), capture_factory=lambda _p: closed).open()
    assert closed.released
    flat = FakeCapture([], size=(0, 0))
    with pytest.raises(SourceError, match="no picture size"):
        VideoFileSource(file_spec(path), capture_factory=lambda _p: flat).open()


# ------------------------------------------------------------------ credentials


def test_credentials_never_print_their_secret() -> None:
    pair = creds.Credentials("admin", SECRET)
    assert SECRET not in repr(pair) and SECRET not in f"{pair}"


def test_a_secret_with_url_syntax_is_encoded_and_survives_a_round_trip() -> None:
    url = creds.with_credentials(
        "rtsp://cam.local:554/stream1?x=1", creds.Credentials("a b", SECRET)
    )
    assert SECRET not in url and "%40" in url  # encoded, not raw
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    assert (unquote(parts.username or ""), unquote(parts.password or "")) == ("a b", SECRET)
    assert parts.hostname == "cam.local" and parts.port == 554 and parts.query == "x=1"


def test_an_ipv6_host_keeps_its_brackets() -> None:
    url = creds.with_credentials("rtsp://[fe80::1]:8554/s", creds.Credentials("u", "p"))
    assert "@[fe80::1]:8554/s" in url


def test_redaction_removes_user_information_and_any_query_that_might_carry_a_token() -> None:
    shown = creds.redact_url("rtsp://admin:hunter2@cam.local:554/live?token=abc123")
    assert shown == "rtsp://cam.local:554/live"


def test_no_credentials_configured_means_none_and_the_url_is_untouched() -> None:
    assert creds.resolve_credentials({}, env={}) is None
    assert creds.with_credentials("rtsp://cam/s", None) == "rtsp://cam/s"


def test_environment_credentials_resolve_and_a_missing_variable_names_only_the_name() -> None:
    got = creds.resolve_credentials(
        {"username_env": "CAM_USER", "password_env": "CAM_PASS"},
        env={"CAM_USER": "u", "CAM_PASS": "p"},
    )
    assert got == creds.Credentials("u", "p")
    with pytest.raises(SourceError) as info:
        creds.resolve_credentials({"username_env": "CAM_USER"}, env={})
    assert "CAM_USER" in str(info.value)


def test_the_credential_manager_wins_over_the_environment_and_a_missing_entry_names_the_ref() -> (
    None
):
    store = {("vigil-88", "gate"): '{"u": "admin", "p": "s3cret"}'}
    got = creds.resolve_credentials(
        {"credential_ref": "gate", "username_env": "X"},
        env={"X": "ignored"},
        get=lambda s, n: store.get((s, n)),
    )
    assert got == creds.Credentials("admin", "s3cret")
    with pytest.raises(SourceError, match="'ghost'"):
        creds.resolve_credentials({"credential_ref": "ghost"}, get=lambda s, n: None)
    with pytest.raises(SourceError, match="unreadable"):
        creds.resolve_credentials({"credential_ref": "gate"}, get=lambda s, n: "not json")


def test_storing_and_deleting_go_through_keyring_and_never_return_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault: dict[tuple[str, str], str] = {}
    fake = types.ModuleType("keyring")
    errors = types.ModuleType("keyring.errors")

    class PasswordDeleteError(Exception):
        pass

    errors.PasswordDeleteError = PasswordDeleteError  # type: ignore[attr-defined]
    fake.set_password = lambda s, n, p: vault.__setitem__((s, n), p)  # type: ignore[attr-defined]
    fake.get_password = lambda s, n: vault.get((s, n))  # type: ignore[attr-defined]

    def delete(s: str, n: str) -> None:
        if (s, n) not in vault:
            raise PasswordDeleteError
        del vault[(s, n)]

    fake.delete_password = delete  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors)

    creds.store_credentials("gate", "admin", SECRET)
    assert creds.has_credentials("gate") is True and creds.has_credentials("nobody") is False
    assert creds.resolve_credentials({"credential_ref": "gate"}) == creds.Credentials(
        "admin", SECRET
    )
    assert creds.delete_credentials("gate") is True and creds.delete_credentials("gate") is False


def test_a_credential_name_is_a_slug_and_a_missing_keyring_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="slug"):
        creds.store_credentials("../escape", "u", "p")
    monkeypatch.setitem(sys.modules, "keyring", None)  # an import of it now fails
    with pytest.raises(SourceError, match="credentials extra"):
        creds.store_credentials("gate", "u", "p")
    with pytest.raises(SourceError, match="credentials extra"):
        creds.resolve_credentials({"credential_ref": "gate"})


# ------------------------------------------------------------------ RTSP


def rtsp_spec(url: str = "rtsp://cam.local:554/live", **options: object) -> SourceSpec:
    return SourceSpec(SourceKind.RTSP, url, options)


class RtspFake:
    def __init__(self, frames: list[object], *, opened: bool = True) -> None:
        self._frames, self._opened = list(frames), opened
        self.released = False

    def isOpened(self) -> bool:
        return self._opened

    def read(self) -> tuple[bool, object]:
        return (True, self._frames.pop(0)) if self._frames else (False, None)

    def get(self, prop: int) -> float:
        return 15.0 if prop == cv2.CAP_PROP_FPS else 0.0

    def release(self) -> None:
        self.released = True


def frame() -> np.ndarray:
    return np.zeros((48, 64, 3), np.uint8)


def test_the_capture_is_opened_with_real_timeouts_and_the_credentials_from_the_environment() -> (
    None
):
    seen: dict[str, object] = {}

    def factory(url: str, params: list[int]) -> RtspFake:
        seen["url"], seen["params"] = url, params
        return RtspFake([frame()])

    source = RtspSource(
        rtsp_spec(username_env="U", password_env="P"),
        capture_factory=factory,
        env={"U": "admin", "P": SECRET},
    )
    info = source.open()
    assert (info.width_px, info.height_px, info.fps) == (64, 48, 15.0)
    assert info.detail == "TCP transport" and source.timing is SourceTiming.LIVE
    assert seen["params"] == [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
        OPEN_TIMEOUT_MS,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
        READ_TIMEOUT_MS,
    ]
    assert "admin:" in str(seen["url"]) and SECRET not in str(seen["url"])  # present, and encoded


def test_the_first_frame_read_to_prove_the_stream_is_returned_by_the_first_read() -> None:
    source = RtspSource(rtsp_spec(), capture_factory=lambda u, p: RtspFake([frame(), frame()]))
    source.open()
    assert source.read() is not None and source.read() is not None and source.is_open


def test_a_camera_that_will_not_connect_is_retryable_and_the_message_holds_no_secret() -> None:
    cap = RtspFake([], opened=False)
    source = RtspSource(
        rtsp_spec("rtsp://cam.local/live?token=abc", username_env="U"),
        capture_factory=lambda u, p: cap,
        env={"U": "admin"},
    )
    with pytest.raises(SourceError) as info:
        source.open()
    text = str(info.value)
    assert info.value.retryable is True and cap.released
    assert "abc" not in text and "cam.local" in text  # the query can carry a token


def test_a_stream_that_opens_but_sends_nothing_is_a_retryable_failure() -> None:
    cap = RtspFake([])
    source = RtspSource(rtsp_spec(), capture_factory=lambda u, p: cap)
    with pytest.raises(SourceError) as info:
        source.open()
    assert info.value.retryable and cap.released


def test_a_timed_out_read_is_transient_so_the_reconnect_machinery_takes_over() -> None:
    source = RtspSource(rtsp_spec(), capture_factory=lambda u, p: RtspFake([frame()]))
    source.open()
    source.read()
    with pytest.raises(TransientSourceError):
        source.read()


def test_an_opencv_error_never_leaks_the_url_it_was_given() -> None:
    def boom(url: str, params: list[int]) -> RtspFake:
        raise cv2.error(f"cannot open {url}")

    source = RtspSource(
        rtsp_spec(credential_ref="gate"),
        capture_factory=boom,
        keyring_get=lambda s, n: '{"u":"a","p":"zzTOPSECRETzz"}',
    )
    with pytest.raises(SourceError) as info:
        source.open()
    assert "zzTOPSECRETzz" not in str(info.value) and info.value.__cause__ is None


def test_reading_a_closed_source_is_an_error_and_close_releases_the_capture() -> None:
    cap = RtspFake([frame()])
    source = RtspSource(rtsp_spec(), capture_factory=lambda u, p: cap)
    with pytest.raises(SourceError):
        source.read()
    source.open()
    source.close()
    assert cap.released and not source.is_open


def test_the_factory_now_builds_both_kinds_and_only_image_remains_unimplemented(
    tmp_path: Path,
) -> None:
    from vigil.core.errors import CapabilityError
    from vigil.ingest.sources.factory import IMPLEMENTED_KINDS, build_source

    assert {SourceKind.VIDEO_FILE, SourceKind.RTSP} <= IMPLEMENTED_KINDS
    assert isinstance(build_source(file_spec(tmp_path / "x.mp4")), VideoFileSource)
    assert isinstance(build_source(rtsp_spec()), RtspSource)
    with pytest.raises(CapabilityError):
        build_source(SourceSpec(SourceKind.IMAGE, "image:x.png", {}))
