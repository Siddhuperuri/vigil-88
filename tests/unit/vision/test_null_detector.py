from __future__ import annotations

import pytest

from tests.support.fakes import StubPixels
from vigil.config.schema.vision import VisionConfig
from vigil.core.capabilities import Capability
from vigil.core.clock import ManualClock
from vigil.core.errors import CapabilityError, InferenceError
from vigil.core.protocols.detector import Detector
from vigil.domain import Frame, FrameMeta, PreparedFrame
from vigil.vision.backends.null import NullDetector
from vigil.vision.factory import IMPLEMENTED_BACKENDS, build_detector


def prepared(i: int = 0, camera: str = "cam-a") -> PreparedFrame:
    clock = ManualClock()
    meta = FrameMeta(camera, 1, i, 0, clock.wall_utc(), 64, 48, None, None)
    return PreparedFrame(Frame(meta, StubPixels()))


def test_it_satisfies_the_detector_protocol(clock: ManualClock) -> None:
    det: Detector = NullDetector(clock)  # static check via annotation; runtime below
    assert det.descriptor.name == "null"


def test_one_empty_result_per_frame_in_order(clock: ManualClock) -> None:
    det = NullDetector(clock)
    frames = [prepared(i, cam) for i, cam in enumerate(["a1", "b2", "a1"])]
    out = det.detect_batch(frames)
    assert len(out) == 3
    assert [r.frame_meta for r in out] == [f.frame.meta for f in frames]  # order and identity
    assert all(r.detections == () for r in out)


def test_results_are_valid_domain_objects(clock: ManualClock) -> None:
    (r,) = NullDetector(clock).detect_batch([prepared()])
    assert r.model.backend == "null" and r.inference_latency_ms >= 0.0


def test_empty_batch_gives_empty_output(clock: ManualClock) -> None:
    assert list(NullDetector(clock).detect_batch([])) == []


def test_it_never_invents_a_detection(clock: ManualClock) -> None:
    det = NullDetector(clock)
    total = sum(len(r.detections) for r in det.detect_batch([prepared(i) for i in range(500)]))
    assert total == 0


def test_it_claims_no_capabilities_because_it_detects_nothing(clock: ManualClock) -> None:
    """D-008: DETECTION must stay unavailable, or modules would appear to work."""
    assert NullDetector(clock).provides == frozenset()
    assert Capability.DETECTION not in NullDetector(clock).provides


def test_latency_comes_from_the_injected_clock(clock: ManualClock) -> None:
    (r,) = NullDetector(clock).detect_batch([prepared()])
    assert r.inference_latency_ms == 0.0  # the manual clock did not advance


def test_warmup_is_a_noop_and_close_is_idempotent(clock: ManualClock) -> None:
    det = NullDetector(clock)
    det.warmup()
    det.close()
    det.close()


def test_use_after_close_is_an_inference_error(clock: ManualClock) -> None:
    det = NullDetector(clock)
    det.close()
    with pytest.raises(InferenceError, match="closed"):
        det.detect_batch([prepared()])
    with pytest.raises(InferenceError, match="closed"):
        det.warmup()


def test_descriptor_is_stable(clock: ManualClock) -> None:
    assert NullDetector(clock).descriptor == NullDetector(ManualClock()).descriptor


# ------------------------------------------------------------------ factory


def test_factory_builds_the_null_backend(clock: ManualClock) -> None:
    assert isinstance(build_detector(VisionConfig(backend="null"), clock), NullDetector)


@pytest.mark.parametrize("backend", ["ultralytics", "onnxruntime", "mock"])
def test_unimplemented_backends_fail_loudly_never_falling_back(
    backend: str, clock: ManualClock
) -> None:
    with pytest.raises(CapabilityError, match=f"{backend}.*not implemented"):
        build_detector(VisionConfig(backend=backend), clock)  # type: ignore[arg-type]


def test_error_names_what_is_implemented(clock: ManualClock) -> None:
    with pytest.raises(CapabilityError, match="null"):
        build_detector(VisionConfig(backend="ultralytics"), clock)
    assert IMPLEMENTED_BACKENDS == {"null"}
