from __future__ import annotations

import threading
from collections.abc import Sequence

import pytest

from tests.support.fakes import StubPixels
from vigil.config.schema.camera import CameraConfig, SourceConfig
from vigil.core.capabilities import Capability
from vigil.core.clock import ManualClock
from vigil.core.errors import InferenceError
from vigil.domain import (
    DetectionSet,
    Frame,
    FrameMeta,
    ModelDescriptor,
    PreparedFrame,
    SourceKind,
)
from vigil.ingest.buffers import LatestFrameSlot
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.observability.probes import SystemProbe
from vigil.pipeline.cameras import camera_from_config
from vigil.pipeline.inference import InferenceWorker
from vigil.pipeline.results import LatestDetections
from vigil.pipeline.sampler import MetricsSampler
from vigil.vision.backends.null import NullDetector

STOP = threading.Event()
MODEL = ModelDescriptor("fake", "1", "fake", None, None, "none", "none", None)


class RecordingDetector:
    descriptor = MODEL
    provides: frozenset[Capability] = frozenset()

    def __init__(self, *, fail: bool = False, wrong_length: bool = False) -> None:
        self.batches: list[list[str]] = []
        self.fail, self.wrong_length = fail, wrong_length

    def warmup(self) -> None: ...

    def detect_batch(self, frames: Sequence[PreparedFrame]) -> Sequence[DetectionSet]:
        if self.fail:
            raise InferenceError("cuda out of memory")
        self.batches.append([f.frame.meta.camera_id for f in frames])
        out = [DetectionSet(f.frame.meta, (), MODEL, 2.0) for f in frames]
        return out[:-1] if self.wrong_length else out

    def close(self) -> None: ...


def frame(camera: str, index: int = 0, *, pts: float | None = None, t: int = 0) -> Frame:
    clock = ManualClock()
    meta = FrameMeta(camera, 1, index, t, clock.wall_utc(), 64, 48, pts, None)
    return Frame(meta, StubPixels())


def worker(
    det: RecordingDetector, cams: dict[str, LatestFrameSlot], clock: ManualClock,
    *, batch: int = 4, metrics: MetricsRegistry | None = None,
) -> tuple[InferenceWorker, MetricsRegistry, LatestDetections]:
    m, results = metrics or MetricsRegistry(), LatestDetections()
    w = InferenceWorker(
        detector=det, channels=cams, results=results, clock=clock, metrics=m,
        logger=get_logger("t"), max_batch_size=batch, idle_wait_ms=1, fps_window_s=5.0,
    )
    return w, m, results


def test_idle_step_infers_nothing(clock: ManualClock) -> None:
    w, _, _ = worker(RecordingDetector(), {"cam-a": LatestFrameSlot()}, clock)
    assert w.step() == 0


def test_frames_from_several_cameras_form_one_batch(clock: ManualClock) -> None:
    slots = {c: LatestFrameSlot() for c in ("cam-a", "cam-b", "cam-c")}
    for c, s in slots.items():
        s.put(frame(c), STOP)
    det = RecordingDetector()
    w, m, results = worker(det, slots, clock)
    assert w.step() == 3
    assert sorted(det.batches[0]) == ["cam-a", "cam-b", "cam-c"]
    assert set(results.snapshot()) == {"cam-a", "cam-b", "cam-c"}
    assert m.counter_total("vigil_frames_inferred_total") == 3


def test_batch_size_is_capped_and_cameras_are_served_fairly(clock: ManualClock) -> None:
    slots = {c: LatestFrameSlot() for c in ("cam-a", "cam-b", "cam-c")}
    det = RecordingDetector()
    w, _, _ = worker(det, slots, clock, batch=2)
    served: list[str] = []
    for _ in range(6):
        for c, s in slots.items():
            s.put(frame(c), STOP)
        w.step()
        served += det.batches[-1]
    assert all(len(b) <= 2 for b in det.batches)
    assert {c: served.count(c) for c in slots} == {"cam-a": 4, "cam-b": 4, "cam-c": 4}


def test_an_inference_error_is_counted_and_logged_not_raised(clock: ManualClock) -> None:
    slot = LatestFrameSlot()
    slot.put(frame("cam-a"), STOP)
    w, m, results = worker(RecordingDetector(fail=True), {"cam-a": slot}, clock)
    assert w.step() == 0
    assert m.counter_total("vigil_inference_errors_total") == 1
    assert results.get("cam-a") is None  # no result is invented


def test_a_detector_that_breaks_the_one_result_per_frame_contract_is_rejected(
    clock: ManualClock,
) -> None:
    slots = {c: LatestFrameSlot() for c in ("cam-a", "cam-b")}
    for c, s in slots.items():
        s.put(frame(c), STOP)
    w, m, results = worker(RecordingDetector(wrong_length=True), slots, clock)
    assert w.step() == 0
    assert m.counter_total("vigil_inference_errors_total") == 1 and results.snapshot() == {}


def test_metrics_are_recorded_for_a_batch(clock: ManualClock) -> None:
    slot = LatestFrameSlot()
    slot.put(frame("cam-a", t=0), STOP)
    clock.advance_ms(30)
    w, m, results = worker(RecordingDetector(), {"cam-a": slot}, clock)
    w.step()
    assert m.histogram("vigil_inference_latency_ms").summary().p50 == 2.0
    assert m.histogram("vigil_capture_to_infer_ms", camera="cam-a").summary().p50 == 30.0
    assert m.gauge("vigil_pipeline_fps").value == pytest.approx(1 / 5.0)
    assert m.gauge("vigil_queue_depth", queue="capture:cam-a").value == 0
    assert results.get("cam-a") is not None


def test_replayed_frames_do_not_report_capture_latency(clock: ManualClock) -> None:
    """A replay camera runs on its own PTS clock; comparing it to the app clock is nonsense."""
    slot = LatestFrameSlot()
    slot.put(frame("cam-a", pts=0.0), STOP)
    w, m, _ = worker(RecordingDetector(), {"cam-a": slot}, clock)
    w.step()
    assert m.histogram("vigil_capture_to_infer_ms", camera="cam-a").summary().count == 0


def test_pipeline_fps_window_forgets_old_frames(clock: ManualClock) -> None:
    slot = LatestFrameSlot()
    w, m, _ = worker(RecordingDetector(), {"cam-a": slot}, clock)
    for _ in range(10):
        slot.put(frame("cam-a"), STOP)
        w.step()
    assert m.gauge("vigil_pipeline_fps").value == pytest.approx(10 / 5.0)
    clock.advance_ms(60_000)
    slot.put(frame("cam-a"), STOP)
    w.step()
    assert m.gauge("vigil_pipeline_fps").value == pytest.approx(1 / 5.0)


def test_run_loops_until_stopped(clock: ManualClock) -> None:
    slot = LatestFrameSlot()
    slot.put(frame("cam-a"), STOP)
    w, m, _ = worker(RecordingDetector(), {"cam-a": slot}, clock)
    stop, beats = threading.Event(), []
    t = threading.Thread(target=w.run, args=(stop, lambda: beats.append(1)))
    t.start()
    for _ in range(500):
        if m.counter_total("vigil_frames_inferred_total") >= 1:
            break
        threading.Event().wait(0.01)
    stop.set()
    t.join(5)
    assert not t.is_alive() and beats and m.counter_total("vigil_frames_inferred_total") == 1


def test_it_works_with_the_real_null_detector(clock: ManualClock) -> None:
    slot = LatestFrameSlot()
    slot.put(frame("cam-a"), STOP)
    m, results = MetricsRegistry(), LatestDetections()
    w = InferenceWorker(
        detector=NullDetector(clock), channels={"cam-a": slot}, results=results, clock=clock,
        metrics=m, logger=get_logger("t"), max_batch_size=4, idle_wait_ms=1, fps_window_s=5.0,
    )
    assert w.step() == 1
    got = results.get("cam-a")
    assert got is not None and got.detections == ()
    assert m.counter_total("vigil_detections_total") == 0


# ------------------------------------------------------------------ camera mapping


def cfg(**src: object) -> CameraConfig:
    return CameraConfig(camera_id="cam-a", source=SourceConfig(**src))  # type: ignore[arg-type]


def test_webcam_config_maps_to_a_domain_camera() -> None:
    cam = camera_from_config(
        cfg(kind="webcam", device_index=2, width_px=1280, height_px=720, fps=30)
    )
    assert cam.source.kind is SourceKind.WEBCAM and cam.source.locator == "webcam:2"
    assert cam.source.options["device_index"] == 2 and cam.source.options["width_px"] == 1280
    assert "url" not in cam.source.options


def test_unset_options_are_omitted_not_zeroed() -> None:
    cam = camera_from_config(cfg(kind="webcam", device_index=0))
    assert "width_px" not in cam.source.options and "fps" not in cam.source.options


def test_rtsp_mapping_carries_credential_variable_names_never_values() -> None:
    cam = camera_from_config(
        cfg(kind="rtsp", url="rtsp://10.0.0.5/s", username_env="CAM_U", password_env="CAM_P")
    )
    assert cam.source.locator == "rtsp://10.0.0.5/s"
    assert cam.source.options == {"username_env": "CAM_U", "password_env": "CAM_P"}


def test_synthetic_and_file_mappings() -> None:
    synth = camera_from_config(cfg(kind="synthetic", frame_count=10, seed=3))
    assert synth.source.options["frame_count"] == 10 and synth.source.options["seed"] == 3
    video = camera_from_config(cfg(kind="video_file", path="clips/a.mp4"))
    assert video.source.kind is SourceKind.VIDEO_FILE and "clips" in video.source.locator


# ------------------------------------------------------------------ sampler


def test_sampler_produces_system_metrics_with_unmeasured_gpu_as_none(clock: ManualClock) -> None:
    m = MetricsRegistry()
    m.histogram("vigil_inference_latency_ms").observe(4.0)
    m.gauge("vigil_pipeline_fps").set(9.5)
    m.gauge("vigil_queue_depth", queue="capture:cam-a").set(2)
    m.counter("vigil_frames_dropped_total", camera="cam-a").inc(3)
    s = MetricsSampler(probe=SystemProbe(), metrics=m, clock=clock, interval_ms=100, history_len=3)
    metric = s.sample()
    assert metric.gpu_utilization_percent is None and metric.gpu_memory_used_mb is None
    assert metric.pipeline_fps == 9.5 and metric.inference_latency_p95_ms == 4.0
    assert metric.frames_dropped_total == 3 and metric.queue_depths == {"queue=capture:cam-a": 2}
    assert m.gauge("vigil_cpu_percent").value is not None


def test_sampler_history_is_bounded_and_latest_is_newest(clock: ManualClock) -> None:
    s = MetricsSampler(
        probe=SystemProbe(), metrics=MetricsRegistry(), clock=clock, interval_ms=100, history_len=3
    )
    assert s.latest() is None
    for _ in range(5):
        clock.advance_ms(100)
        s.sample()
    assert len(s.history()) == 3 and s.latest() == s.history()[-1]


def test_unmeasured_pipeline_values_stay_none(clock: ManualClock) -> None:
    s = MetricsSampler(
        probe=SystemProbe(), metrics=MetricsRegistry(), clock=clock, interval_ms=100, history_len=2
    )
    m = s.sample()
    assert m.pipeline_fps is None and m.inference_latency_p50_ms is None
