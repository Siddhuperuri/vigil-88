"""The ONNX backend against REAL onnxruntime, using generated models with known output."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from tests.support.fakes import StubPixels
from tests.support.onnx_models import PlantedBox, record, write_model
from vigil.config.schema.vision import VisionConfig
from vigil.core.capabilities import Capability
from vigil.core.clock import ManualClock
from vigil.core.errors import CapabilityError, InferenceError
from vigil.domain import Frame, FrameMeta, ObjectClass, PreparedFrame
from vigil.ingest.pixel_buffer import NumpyPixelBuffer
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.vision.backends.onnxruntime import OnnxDetector, Session
from vigil.vision.device import CPU_PROVIDER, CUDA_PROVIDER, available_providers
from vigil.vision.preprocess import F32

CPU_ONLY = (CPU_PROVIDER,)
PERSON_BOX = PlantedBox(cx=200, cy=150, w=100, h=80, class_id=0, score=0.9)


def cfg(**kw: object) -> VisionConfig:
    base: dict[str, object] = {"backend": "onnxruntime", "weights": "fake_yolox", "device": "cpu"}
    return VisionConfig(**{**base, **kw})  # type: ignore[arg-type]


def detector(
    tmp_path: Path,
    clock: ManualClock,
    *,
    boxes: Sequence[PlantedBox] = (PERSON_BOX,),
    metrics: MetricsRegistry | None = None,
    size: int = 416,
    **kw: object,
) -> OnnxDetector:
    write_model(tmp_path, size=size, boxes=boxes)
    return OnnxDetector(
        cfg=cfg(**kw),
        models_dir=tmp_path,
        clock=clock,
        logger=get_logger("t"),
        metrics=metrics,
        providers=CPU_ONLY,
    )


def prepared(width: int = 640, height: int = 480, index: int = 0) -> PreparedFrame:
    pixels = np.full((height, width, 3), 90, dtype=np.uint8)
    clock = ManualClock()
    meta = FrameMeta("cam-a", 1, index, 0, clock.wall_utc(), width, height, None, None)
    return PreparedFrame(Frame(meta, NumpyPixelBuffer(pixels)))


# ------------------------------------------------------------------ loading and inference


def test_loads_a_model_and_reports_an_honest_descriptor(tmp_path: Path, clock: ManualClock) -> None:
    det = detector(tmp_path, clock)
    d = det.descriptor
    assert (d.name, d.backend, d.input_size_px, d.device, d.precision) == (
        "fake_yolox",
        "onnxruntime",
        416,
        "cpu",
        "fp32",
    )
    assert d.weights_sha256 and len(d.weights_sha256) == 64 and d.license == "CC0-1.0"
    assert det.provides == frozenset({Capability.DETECTION})


def test_real_inference_returns_the_planted_detection_in_source_pixels(
    tmp_path: Path, clock: ManualClock
) -> None:
    det = detector(tmp_path, clock)
    (result,) = det.detect_batch([prepared()])
    (d,) = result.detections
    assert d.object_class is ObjectClass.PERSON and d.native_label == "person"
    assert (d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2) == pytest.approx(
        (150 / 0.65, 110 / 0.65, 250 / 0.65, 190 / 0.65), abs=0.5
    )
    assert result.model == det.descriptor and result.frame_meta.frame_index == 0


def test_one_result_per_frame_in_order_with_identity_preserved(
    tmp_path: Path, clock: ManualClock
) -> None:
    det = detector(tmp_path, clock)
    frames = [prepared(index=i) for i in (7, 8, 9)]
    out = det.detect_batch(frames)
    assert [r.frame_meta.frame_index for r in out] == [7, 8, 9]
    assert all(len(r.detections) == 1 for r in out)


def test_batch_of_n_equals_n_single_frame_calls(tmp_path: Path, clock: ManualClock) -> None:
    """These exports have a static batch of 1, so batching must be a pure loop."""
    det = detector(tmp_path, clock)
    frames = [prepared(index=i) for i in range(3)]
    together = det.detect_batch(frames)
    separately = [det.detect_batch([f])[0] for f in frames]
    assert [r.detections for r in together] == [r.detections for r in separately]


def test_an_empty_scene_yields_valid_empty_results(tmp_path: Path, clock: ManualClock) -> None:
    det = detector(tmp_path, clock, boxes=())
    (result,) = det.detect_batch([prepared()])
    assert result.detections == () and result.inference_latency_ms >= 0.0


def test_an_empty_batch_gives_an_empty_result(tmp_path: Path, clock: ManualClock) -> None:
    assert list(detector(tmp_path, clock).detect_batch([])) == []


def test_inference_is_deterministic(tmp_path: Path, clock: ManualClock) -> None:
    det = detector(tmp_path, clock)
    a = det.detect_batch([prepared()])[0].detections
    b = det.detect_batch([prepared()])[0].detections
    assert a == b


def test_frames_of_any_resolution_map_back_to_their_own_pixels(
    tmp_path: Path, clock: ManualClock
) -> None:
    det = detector(tmp_path, clock, boxes=[PlantedBox(200, 150, 100, 80, 0, 0.9)])
    wide = det.detect_batch([prepared(1920, 1080)])[0].detections[0].bbox
    small = det.detect_batch([prepared(320, 240)])[0].detections[0].bbox
    assert wide.x2 <= 1920 and wide.y2 <= 1080 and small.x2 <= 320 and small.y2 <= 240
    assert wide.width / small.width == pytest.approx(1920 / 320, rel=0.02)  # scales with the frame


def test_stage_latencies_are_recorded_per_stage(tmp_path: Path, clock: ManualClock) -> None:
    metrics = MetricsRegistry()
    det = detector(tmp_path, clock, metrics=metrics)
    det.detect_batch([prepared()])
    for stage in ("preprocess", "infer", "postprocess"):
        assert metrics.histogram("vigil_stage_latency_ms", stage=stage).summary().count == 1
    assert metrics.gauge("vigil_detector_warmup_ms").value is not None


def test_unmapped_detections_are_counted(tmp_path: Path, clock: ManualClock) -> None:
    metrics = MetricsRegistry()
    book = PlantedBox(200, 150, 100, 80, class_id=73, score=0.9)
    det = detector(tmp_path, clock, boxes=[book], metrics=metrics)
    assert det.detect_batch([prepared()])[0].detections == ()
    assert metrics.counter("vigil_unmapped_labels_total", label="book").value == 1


# ------------------------------------------------------------------ invalid results


def test_a_nan_prediction_tensor_is_an_inference_error(tmp_path: Path, clock: ManualClock) -> None:
    bad = np.zeros((1, 3549, 85), dtype=np.float32)
    bad[0, 5, 4] = np.nan
    write_model(tmp_path, constant_output=bad)
    det = OnnxDetector(
        cfg=cfg(), models_dir=tmp_path, clock=clock, logger=get_logger("t"), providers=CPU_ONLY
    )
    with pytest.raises(InferenceError, match="NaN or infinite"):
        det.detect_batch([prepared()])


def test_a_wrongly_shaped_output_is_an_inference_error(tmp_path: Path, clock: ManualClock) -> None:
    write_model(tmp_path, constant_output=np.zeros((1, 100, 85), dtype=np.float32))
    det = OnnxDetector(
        cfg=cfg(), models_dir=tmp_path, clock=clock, logger=get_logger("t"), providers=CPU_ONLY
    )
    with pytest.raises(InferenceError, match="shape"):
        det.detect_batch([prepared()])


class ShortPixels(StubPixels):
    """Claims 640x480x3 but hands over far fewer bytes: a corrupt or truncated buffer."""

    def buffer(self) -> memoryview:
        return memoryview(b"\x00" * 1000)


def test_unreadable_pixels_are_an_inference_error(tmp_path: Path, clock: ManualClock) -> None:
    det = detector(tmp_path, clock)
    meta = FrameMeta("cam-a", 1, 0, 0, clock.wall_utc(), 640, 480, None, None)
    corrupt = PreparedFrame(Frame(meta, ShortPixels(640, 480)))
    with pytest.raises(InferenceError, match="cannot read"):
        det.detect_batch([corrupt])


# ------------------------------------------------------------------ model loading failures


def test_a_missing_model_is_a_capability_error_naming_the_fix(
    tmp_path: Path, clock: ManualClock
) -> None:
    with pytest.raises(CapabilityError, match="vigil models fetch yolox_s"):
        OnnxDetector(
            cfg=cfg(weights="yolox_s"),
            models_dir=tmp_path,
            clock=clock,
            logger=get_logger("t"),
            providers=CPU_ONLY,
        )


def test_an_unlisted_model_file_is_refused(tmp_path: Path, clock: ManualClock) -> None:
    write_model(tmp_path, register=False)
    with pytest.raises(CapabilityError, match=r"not listed in MANIFEST.json"):
        OnnxDetector(
            cfg=cfg(), models_dir=tmp_path, clock=clock, logger=get_logger("t"), providers=CPU_ONLY
        )


def test_a_model_that_changed_after_it_was_recorded_is_refused(
    tmp_path: Path, clock: ManualClock
) -> None:
    path = write_model(tmp_path)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF  # flip one byte, keep the size
    path.write_bytes(bytes(data))
    with pytest.raises(CapabilityError, match="failed its integrity check"):
        OnnxDetector(
            cfg=cfg(), models_dir=tmp_path, clock=clock, logger=get_logger("t"), providers=CPU_ONLY
        )


def test_a_truncated_model_is_refused_by_size(tmp_path: Path, clock: ManualClock) -> None:
    path = write_model(tmp_path)
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(CapabilityError, match="bytes but the manifest records"):
        OnnxDetector(
            cfg=cfg(), models_dir=tmp_path, clock=clock, logger=get_logger("t"), providers=CPU_ONLY
        )


def test_a_file_that_is_not_an_onnx_model_fails_cleanly(tmp_path: Path, clock: ManualClock) -> None:
    junk = tmp_path / "fake_yolox.onnx"
    junk.write_bytes(b"this is not a model" * 50)
    record(tmp_path, junk)  # integrity is fine; the CONTENT is garbage
    with pytest.raises(CapabilityError, match=r"failed to load fake_yolox.onnx on CPU"):
        OnnxDetector(
            cfg=cfg(), models_dir=tmp_path, clock=clock, logger=get_logger("t"), providers=CPU_ONLY
        )


def test_a_model_whose_input_size_disagrees_with_the_config_is_refused(
    tmp_path: Path, clock: ManualClock
) -> None:
    write_model(tmp_path, size=416)
    with pytest.raises(CapabilityError, match="fixed 416px input"):
        OnnxDetector(
            cfg=cfg(input_size_px=640),
            models_dir=tmp_path,
            clock=clock,
            logger=get_logger("t"),
            providers=CPU_ONLY,
        )


def test_fp16_is_refused_rather_than_silently_ignored(tmp_path: Path, clock: ManualClock) -> None:
    write_model(tmp_path)
    with pytest.raises(CapabilityError, match="fp16"):
        OnnxDetector(
            cfg=cfg(precision="fp16"),
            models_dir=tmp_path,
            clock=clock,
            logger=get_logger("t"),
            providers=CPU_ONLY,
        )


def test_weights_must_be_a_bare_name_not_a_path(tmp_path: Path, clock: ManualClock) -> None:
    for bad in ("../x.onnx", "sub/x.onnx", "C:/x.onnx", ".."):
        with pytest.raises(CapabilityError, match="not a path"):
            OnnxDetector(
                cfg=cfg(weights=bad),
                models_dir=tmp_path,
                clock=clock,
                logger=get_logger("t"),
                providers=CPU_ONLY,
            )


def test_the_onnx_backend_requires_weights() -> None:
    with pytest.raises(ValueError, match=r"requires vision.weights"):
        VisionConfig(backend="onnxruntime")


# ------------------------------------------------------------------ lifecycle


def test_close_is_idempotent_and_use_after_close_fails(tmp_path: Path, clock: ManualClock) -> None:
    det = detector(tmp_path, clock)
    det.close()
    det.close()
    with pytest.raises(InferenceError, match="closed"):
        det.detect_batch([prepared()])
    with pytest.raises(InferenceError, match="closed"):
        det.warmup()


# ------------------------------------------------------------------ device and fallback policy


class FakeSession:
    """A stand-in session: scripted providers and scripted run failures."""

    def __init__(self, size: int, providers: tuple[str, ...], fail_runs: int = 0) -> None:
        self._size, self._providers, self.fail_runs, self.runs = size, providers, fail_runs, 0

    @property
    def input_size_px(self) -> int:
        return self._size

    @property
    def active_providers(self) -> tuple[str, ...]:
        return self._providers

    def run(self, batch: F32) -> F32:
        self.runs += 1
        if self.fail_runs:
            self.fail_runs -= 1
            raise RuntimeError("CUDA error: out of memory")
        from tests.support.onnx_models import raw_predictions

        return raw_predictions(self._size, [PERSON_BOX])


def fake_factory(cuda: FakeSession | None, cpu: FakeSession) -> object:
    def factory(path: Path, kind: str, cfg: VisionConfig) -> Session:
        if kind == "cuda":
            if cuda is None:
                raise RuntimeError("CUDA init failed: cudnn missing")
            return cuda
        return cpu

    return factory


def with_fakes(tmp_path: Path, clock: ManualClock, factory: object, **kw: object) -> OnnxDetector:
    write_model(tmp_path)
    return OnnxDetector(
        cfg=cfg(device="cuda", **kw),
        models_dir=tmp_path,
        clock=clock,
        logger=get_logger("t"),
        providers=(CUDA_PROVIDER, CPU_PROVIDER),
        session_factory=factory,  # type: ignore[arg-type]
    )


def test_cuda_is_used_when_it_works(tmp_path: Path, clock: ManualClock) -> None:
    cuda = FakeSession(416, (CUDA_PROVIDER, CPU_PROVIDER))
    det = with_fakes(tmp_path, clock, fake_factory(cuda, FakeSession(416, (CPU_PROVIDER,))))
    assert det.descriptor.device.startswith("cuda") and det.descriptor.precision == "fp32+tf32"
    det.detect_batch([prepared()])
    assert cuda.runs >= 3  # two warm-ups plus the frame


def test_a_cuda_init_failure_falls_back_to_cpu_loudly_when_allowed(
    tmp_path: Path, clock: ManualClock
) -> None:
    det = with_fakes(tmp_path, clock, fake_factory(None, FakeSession(416, (CPU_PROVIDER,))))
    assert "fell back" in det.descriptor.device and det.descriptor.precision == "fp32"
    assert "cudnn missing" in det.device_reason
    assert len(det.detect_batch([prepared()])[0].detections) == 1  # and it still detects


def test_a_cuda_init_failure_is_an_error_when_fallback_is_forbidden(
    tmp_path: Path, clock: ManualClock
) -> None:
    with pytest.raises(CapabilityError, match="allow_cpu_fallback is false"):
        with_fakes(
            tmp_path,
            clock,
            fake_factory(None, FakeSession(416, (CPU_PROVIDER,))),
            allow_cpu_fallback=False,
        )


def test_ort_silently_dropping_cuda_is_detected_not_trusted(
    tmp_path: Path, clock: ManualClock
) -> None:
    silent = FakeSession(416, (CPU_PROVIDER,))  # created "fine" but CUDA is not active
    det = with_fakes(tmp_path, clock, fake_factory(silent, FakeSession(416, (CPU_PROVIDER,))))
    assert "silently fell back" in det.device_reason and "fell back" in det.descriptor.device


def test_a_failing_cuda_warmup_is_caught_at_load_time(tmp_path: Path, clock: ManualClock) -> None:
    broken = FakeSession(416, (CUDA_PROVIDER, CPU_PROVIDER), fail_runs=99)
    det = with_fakes(tmp_path, clock, fake_factory(broken, FakeSession(416, (CPU_PROVIDER,))))
    assert "warm-up run failed" in det.device_reason and "fell back" in det.descriptor.device


def test_a_runtime_cuda_failure_switches_to_cpu_and_retries_the_frame(
    tmp_path: Path, clock: ManualClock
) -> None:
    flaky = FakeSession(416, (CUDA_PROVIDER, CPU_PROVIDER))
    det = with_fakes(tmp_path, clock, fake_factory(flaky, FakeSession(416, (CPU_PROVIDER,))))
    flaky.fail_runs = 1  # fails on the first REAL frame, after a clean warm-up
    (result,) = det.detect_batch([prepared()])
    assert len(result.detections) == 1  # the frame was not lost
    assert "fell back" in det.descriptor.device and "fell back" in result.model.device


def test_a_runtime_cuda_failure_raises_when_fallback_is_forbidden(
    tmp_path: Path, clock: ManualClock
) -> None:
    flaky = FakeSession(416, (CUDA_PROVIDER, CPU_PROVIDER))
    det = with_fakes(
        tmp_path,
        clock,
        fake_factory(flaky, FakeSession(416, (CPU_PROVIDER,))),
        allow_cpu_fallback=False,
    )
    flaky.fail_runs = 1
    with pytest.raises(InferenceError, match="out of memory"):
        det.detect_batch([prepared()])


def test_failing_on_cuda_and_again_on_cpu_is_an_inference_error(
    tmp_path: Path, clock: ManualClock
) -> None:
    flaky = FakeSession(416, (CUDA_PROVIDER, CPU_PROVIDER))
    doomed_cpu = FakeSession(416, (CPU_PROVIDER,))
    det = with_fakes(tmp_path, clock, fake_factory(flaky, doomed_cpu))
    flaky.fail_runs, doomed_cpu.fail_runs = 1, 99
    with pytest.raises(InferenceError, match="and again on CPU"):
        det.detect_batch([prepared()])


def test_cuda_requested_but_absent_follows_the_policy(tmp_path: Path, clock: ManualClock) -> None:
    write_model(tmp_path)
    det = OnnxDetector(
        cfg=cfg(device="cuda"),
        models_dir=tmp_path,
        clock=clock,
        logger=get_logger("t"),
        providers=CPU_ONLY,
    )
    assert "fell back" in det.descriptor.device and "onnx-gpu" in det.device_reason
    with pytest.raises(CapabilityError, match="allow_cpu_fallback is false"):
        OnnxDetector(
            cfg=cfg(device="cuda", allow_cpu_fallback=False),
            models_dir=tmp_path,
            clock=clock,
            logger=get_logger("t"),
            providers=CPU_ONLY,
        )


def test_auto_without_cuda_uses_cpu_and_says_why(tmp_path: Path, clock: ManualClock) -> None:
    write_model(tmp_path)
    det = OnnxDetector(
        cfg=cfg(device="auto"),
        models_dir=tmp_path,
        clock=clock,
        logger=get_logger("t"),
        providers=CPU_ONLY,
    )
    assert det.descriptor.device == "cpu (fell back from cuda)" and "Using CPU" in det.device_reason


# ------------------------------------------------------------------ the real GPU


@pytest.mark.gpu
@pytest.mark.skipif(CUDA_PROVIDER not in available_providers(), reason="no CUDA provider installed")
def test_real_cuda_gives_the_same_detections_as_cpu(tmp_path: Path, clock: ManualClock) -> None:
    write_model(tmp_path, boxes=[PERSON_BOX])
    common = {"models_dir": tmp_path, "clock": clock, "logger": get_logger("t")}
    cpu = OnnxDetector(cfg=cfg(device="cpu"), providers=CPU_ONLY, **common)  # type: ignore[arg-type]
    gpu = OnnxDetector(cfg=cfg(device="cuda", allow_cpu_fallback=False), **common)  # type: ignore[arg-type]
    assert gpu.descriptor.device.startswith("cuda:0") and "fell back" not in gpu.device_reason
    a = cpu.detect_batch([prepared()])[0].detections
    b = gpu.detect_batch([prepared()])[0].detections
    assert [d.object_class for d in a] == [d.object_class for d in b]
    for x, y in zip(a, b, strict=True):
        assert (x.bbox.x1, x.bbox.y1, x.bbox.x2, x.bbox.y2) == pytest.approx(
            (y.bbox.x1, y.bbox.y1, y.bbox.x2, y.bbox.y2), abs=0.5
        )
    gpu.close()
    cpu.close()
