"""Application lifecycle: startup, steady state, failure containment, shutdown."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

import pytest

from tests.support.fakes import BlockUntil, ScriptedSource, raw_frame
from vigil.config.loader import LoadedConfig
from vigil.core.capabilities import Capability
from vigil.core.clock import ManualClock
from vigil.core.errors import CapabilityError, LifecycleError, SourceError
from vigil.core.protocols.detector import Detector
from vigil.core.protocols.source import FrameSource, SourceTiming
from vigil.domain import Camera, CameraState, DetectionSet, FrameMeta, PreparedFrame
from vigil.pipeline.app import Application
from vigil.pipeline.health import HealthLevel
from vigil.pipeline.snapshot import AppState
from vigil.pipeline.workers import THREAD_PREFIX
from vigil.vision.backends.null import NullDetector

MakeConfig = Callable[..., LoadedConfig]
SYNTH = {"camera_id": "synth", "source": {"kind": "synthetic", "frame_count": 40}}


def vigil_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith(THREAD_PREFIX)]


def wait_for(cond: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def app_for(cfg: LoadedConfig, **kw: object) -> Application:
    return Application(cfg.settings, cfg.paths, **kw)  # type: ignore[arg-type]


class RecordingDetector:
    """Wraps the null detector, recording every frame identity it is asked about."""

    def __init__(self, clock: ManualClock) -> None:
        self._inner = NullDetector(clock)
        self.metas: list[FrameMeta] = []

    @property
    def descriptor(self):  # type: ignore[no-untyped-def]
        return self._inner.descriptor

    @property
    def provides(self) -> frozenset[Capability]:
        return self._inner.provides

    def warmup(self) -> None:
        self._inner.warmup()

    def detect_batch(self, frames: Sequence[PreparedFrame]) -> Sequence[DetectionSet]:
        self.metas += [f.frame.meta for f in frames]
        return self._inner.detect_batch(frames)

    def close(self) -> None:
        self._inner.close()


# ------------------------------------------------------------------ lifecycle


def test_full_lifecycle_states(make_config: MakeConfig) -> None:
    app = app_for(make_config({"cameras": [SYNTH]}))
    assert app.state is AppState.CREATED
    app.start()
    assert app.state is AppState.RUNNING
    report = app.stop()
    assert app.state is AppState.STOPPED and report.clean and report.stragglers == ()
    assert vigil_threads() == []


def test_context_manager_starts_and_stops(make_config: MakeConfig) -> None:
    with app_for(make_config({"cameras": [SYNTH]})) as app:
        assert app.state is AppState.RUNNING
    assert app.state is AppState.STOPPED and vigil_threads() == []


def test_cannot_start_twice(make_config: MakeConfig) -> None:
    app = app_for(make_config({"cameras": [SYNTH]}))
    app.start()
    try:
        with pytest.raises(LifecycleError, match="cannot start"):
            app.start()
    finally:
        app.stop()


def test_stop_is_idempotent_and_returns_the_same_report(make_config: MakeConfig) -> None:
    app = app_for(make_config({"cameras": [SYNTH]}))
    app.start()
    first = app.stop()
    assert app.stop() is first


def test_stop_before_start_is_harmless(make_config: MakeConfig) -> None:
    app = app_for(make_config())
    assert app.stop().clean and app.state is AppState.STOPPED
    with pytest.raises(LifecycleError):
        app.start()  # a stopped application is not restartable


def test_an_application_with_no_cameras_runs_idle(make_config: MakeConfig) -> None:
    app = app_for(make_config())
    app.start()
    try:
        snap = app.snapshot()
        assert snap.state is AppState.RUNNING and snap.cameras == ()
        assert snap.health.level is HealthLevel.OK
    finally:
        app.stop()


# ------------------------------------------------------------------ the real data path


def test_frames_flow_from_a_source_through_the_null_detector(make_config: MakeConfig) -> None:
    app = app_for(make_config({"cameras": [SYNTH]}))
    app.start()
    try:
        assert app.wait_until_drained(30)
        snap = app.snapshot()
    finally:
        assert app.stop().clean
    cam = snap.cameras[0]
    assert cam.frames_received == 40 and snap.frames_inferred == 40
    assert cam.frames_skipped == 0 and cam.frames_dropped == 0  # paced: nothing is lost
    assert snap.detections_total == 0
    assert cam.state is CameraState.OFFLINE and cam.detail == "end of stream"
    assert snap.detector is not None and snap.detector.name == "null"


def test_null_detector_grants_no_capabilities(make_config: MakeConfig) -> None:
    app = app_for(make_config({"cameras": [SYNTH]}))
    app.start()
    try:
        assert app.capabilities.available() == frozenset()
        assert not any(s.available for s in app.snapshot().capabilities)
    finally:
        app.stop()


def test_a_detector_that_provides_capabilities_grants_them(make_config: MakeConfig) -> None:
    class Capable(NullDetector):
        @property
        def provides(self) -> frozenset[Capability]:
            return frozenset({Capability.DETECTION})

    app = app_for(make_config(), detector_factory=lambda s, c: Capable(c))
    app.start()
    try:
        status = app.capabilities.status(Capability.DETECTION)
        assert status.available and status.provider == "null"
    finally:
        app.stop()


def test_replay_is_deterministic(make_config: MakeConfig) -> None:
    """Same clip, same clock => identical frame identities, timestamps and results."""

    def run_once() -> list[tuple[object, ...]]:
        clock = ManualClock()
        det = RecordingDetector(clock)
        cfg = make_config({"cameras": [SYNTH]})
        app = app_for(cfg, clock=clock, detector_factory=lambda s, c: det)
        app.start()
        try:
            assert app.wait_until_drained(30)
        finally:
            app.stop()
        return [
            (m.camera_id, m.stream_epoch, m.frame_index, m.t_monotonic_ns, m.wall_utc,
             m.source_pts_ms, m.width_px, m.height_px)
            for m in det.metas
        ]

    a, b = run_once(), run_once()
    assert len(a) == 40 and a == b
    pts = [row[5] for row in a]
    assert pts == sorted(pts) and pts[1] - pts[0] == pytest.approx(1000 / 30)


def test_multiple_cameras_run_independently(make_config: MakeConfig) -> None:
    cams = [
        {"camera_id": f"synth-{i}", "source": {"kind": "synthetic", "frame_count": 25}}
        for i in range(3)
    ]
    app = app_for(make_config({"cameras": cams}))
    app.start()
    try:
        assert app.wait_until_drained(30)
        snap = app.snapshot()
    finally:
        app.stop()
    assert [c.frames_received for c in snap.cameras] == [25, 25, 25]
    assert snap.frames_inferred == 75


# ------------------------------------------------------------------ failure containment


def test_an_unimplemented_source_fails_that_camera_not_the_application(
    make_config: MakeConfig,
) -> None:
    streaming = {"camera_id": "synth", "source": {"kind": "synthetic"}}  # never ends
    cams = [{"camera_id": "lobby", "source": {"kind": "rtsp", "url": "rtsp://10.0.0.5/s"}}, streaming]
    app = app_for(make_config({"cameras": cams}))
    app.start()
    try:
        assert app.state is AppState.RUNNING
        assert wait_for(lambda: app.snapshot().frames_inferred >= 20)
        snap = app.snapshot()
    finally:
        app.stop()
    by_id = {c.camera_id: c for c in snap.cameras}
    assert by_id["lobby"].state is CameraState.FAILED
    assert "not implemented" in (by_id["lobby"].detail or "")
    assert by_id["synth"].frames_received >= 20  # the healthy camera was unaffected
    assert snap.health.level is HealthLevel.DEGRADED
    assert any("lobby" in issue for issue in snap.health.issues)


def test_nothing_left_streaming_is_failed_even_if_one_camera_ended_cleanly(
    make_config: MakeConfig,
) -> None:
    cams = [{"camera_id": "lobby", "source": {"kind": "rtsp", "url": "rtsp://10.0.0.5/s"}}, SYNTH]
    app = app_for(make_config({"cameras": cams}))
    app.start()
    try:
        assert app.wait_until_drained(30)  # synth reaches end of stream
        assert app.snapshot().health.level is HealthLevel.FAILED
    finally:
        app.stop()


def test_when_every_camera_fails_the_system_reports_failed(make_config: MakeConfig) -> None:
    cams = [{"camera_id": "lobby", "source": {"kind": "rtsp", "url": "rtsp://10.0.0.5/s"}}]
    app = app_for(make_config({"cameras": cams}))
    app.start()
    try:
        assert app.snapshot().health.level is HealthLevel.FAILED
    finally:
        app.stop()


def test_a_disabled_camera_gets_no_worker_and_is_reported_disabled(
    make_config: MakeConfig,
) -> None:
    cams = [{"camera_id": "off-cam", "enabled": False, "source": {"kind": "synthetic"}}]
    app = app_for(make_config({"cameras": cams}))
    app.start()
    try:
        snap = app.snapshot()
        assert snap.cameras[0].state is CameraState.DISABLED
        assert not any(w.name.startswith("capture:") for w in snap.workers)
        assert snap.health.level is HealthLevel.OK
    finally:
        app.stop()


def test_a_missing_capture_extra_is_reported_as_an_actionable_camera_failure(
    make_config: MakeConfig,
) -> None:
    def no_cv2(_: Camera) -> FrameSource:
        raise CapabilityError("webcam source needs OpenCV/NumPy: uv sync --extra capture")

    app = app_for(
        make_config({"cameras": [{"camera_id": "cam-w", "source": {"kind": "webcam", "device_index": 0}}]}),
        source_factory=no_cv2,
    )
    app.start()
    try:
        cam = app.snapshot().cameras[0]
        assert cam.state is CameraState.FAILED and "uv sync --extra capture" in (cam.detail or "")
    finally:
        app.stop()


def test_an_unimplemented_detector_backend_fails_startup_loudly(make_config: MakeConfig) -> None:
    app = app_for(make_config({"vision.backend": "ultralytics"}))
    with pytest.raises(CapabilityError, match="ultralytics"):
        app.start()
    assert app.state is AppState.FAILED
    assert app.stop().clean and vigil_threads() == []


def test_an_unwritable_data_dir_fails_startup(tmp_path, make_config: MakeConfig) -> None:  # type: ignore[no-untyped-def]
    blocker = tmp_path / "file"
    blocker.write_text("x")
    app = app_for(make_config({"paths.data_dir": str(blocker / "child")}))
    with pytest.raises(OSError):
        app.start()
    assert app.state is AppState.FAILED and vigil_threads() == []


def test_a_flaky_camera_reconnects_and_keeps_serving(make_config: MakeConfig) -> None:
    sources: list[ScriptedSource] = []

    def factory(_: Camera) -> FrameSource:
        src = ScriptedSource(
            [raw_frame(), raw_frame(), SourceError("usb unplugged"), raw_frame(), raw_frame()],
            timing=SourceTiming.LIVE,
        )
        sources.append(src)
        return src

    app = app_for(
        make_config({"cameras": [{"camera_id": "cam-w", "source": {"kind": "webcam", "device_index": 0}}]}),
        source_factory=factory,
    )
    app.start()
    try:
        assert wait_for(lambda: sources and sources[0].open_calls >= 2)
        assert wait_for(lambda: app.snapshot().cameras[0].frames_received >= 4)
        snap = app.snapshot()
    finally:
        app.stop()
    assert snap.cameras[0].stream_epoch == 2  # the reconnect started a new epoch
    assert snap.frames_inferred >= 1


def test_a_worker_that_will_not_stop_is_reported_by_name(make_config: MakeConfig) -> None:
    release = threading.Event()
    src = ScriptedSource([BlockUntil(release)])
    app = app_for(
        make_config(
            {
                "cameras": [{"camera_id": "stuck", "source": {"kind": "webcam", "device_index": 0}}],
                "pipeline.shutdown_timeout_ms": 100,
            }
        ),
        source_factory=lambda _: src,
    )
    app.start()
    try:
        assert wait_for(lambda: src.read_calls >= 1)
        report = app.stop()
        assert not report.clean and report.stragglers == ("capture:stuck",)
        assert app.state is AppState.STOPPED  # shutdown completes and reports; it never hangs
    finally:
        release.set()
    assert wait_for(lambda: vigil_threads() == [])


# ------------------------------------------------------------------ observation


def test_snapshot_reports_uptime_workers_and_system_metrics(make_config: MakeConfig) -> None:
    clock = ManualClock()
    app = app_for(make_config({"cameras": [SYNTH]}), clock=clock)
    assert app.snapshot().detector is None and app.snapshot().uptime_ms is None
    app.start()
    try:
        clock.advance_ms(1500)
        assert wait_for(lambda: app.snapshot().system is not None)
        snap = app.snapshot()
        names = {w.name for w in snap.workers}
        assert {"inference", "watchdog", "sampler", "capture:synth"} <= names
        assert snap.uptime_ms == pytest.approx(1500.0)
        assert snap.system is not None and snap.system.gpu_utilization_percent is None
    finally:
        app.stop()


def test_camera_state_changes_are_logged_and_counted(make_config: MakeConfig) -> None:
    app = app_for(make_config({"cameras": [SYNTH]}))
    app.start()
    try:
        assert app.wait_until_drained(30)
    finally:
        app.stop()
    assert app.metrics.counter("vigil_camera_state_changes_total", camera="synth", state="online").value == 1
    assert app.metrics.counter("vigil_camera_state_changes_total", camera="synth", state="offline").value == 1


def test_detection_results_are_published_for_readers(make_config: MakeConfig) -> None:
    app = app_for(make_config({"cameras": [SYNTH]}))
    app.start()
    try:
        assert app.wait_until_drained(30)
        latest = app.results.get("synth")
    finally:
        app.stop()
    assert latest is not None and latest.frame_meta.frame_index == 39 and latest.detections == ()


def test_detector_type_is_only_the_protocol(make_config: MakeConfig) -> None:
    """The application depends on the Detector protocol, not on a concrete class."""
    det: Detector = NullDetector(ManualClock())
    app = app_for(make_config(), detector_factory=lambda s, c: det)
    app.start()
    app.stop()
