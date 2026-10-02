"""The null detector: a real Detector implementation that detects nothing.

It returns valid, empty DetectionSets. It never invents a detection, and because it detects
nothing it grants NO capabilities: DETECTION stays unavailable, so no module that needs
detections can appear to work (D-008). Its purpose is to prove the capture -> detector ->
result path end to end without a model.
"""

from __future__ import annotations

from collections.abc import Sequence

from vigil.core.capabilities import Capability
from vigil.core.clock import Clock
from vigil.core.errors import InferenceError
from vigil.core.units import ns_to_ms
from vigil.domain.detection import DetectionSet, ModelDescriptor, PreparedFrame

NULL_DESCRIPTOR = ModelDescriptor(
    name="null",
    version="1",
    backend="null",
    weights_sha256=None,
    input_size_px=None,
    precision="none",
    device="none",
    license=None,
)


class NullDetector:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._closed = False

    @property
    def descriptor(self) -> ModelDescriptor:
        return NULL_DESCRIPTOR

    @property
    def provides(self) -> frozenset[Capability]:
        return frozenset()

    def _require_open(self) -> None:
        if self._closed:
            raise InferenceError("the null detector is closed")

    def warmup(self) -> None:
        self._require_open()

    def detect_batch(self, frames: Sequence[PreparedFrame]) -> Sequence[DetectionSet]:
        self._require_open()
        start_ns = self._clock.monotonic_ns()
        elapsed_ms = ns_to_ms(self._clock.monotonic_ns() - start_ns)
        return [DetectionSet(f.frame.meta, (), NULL_DESCRIPTOR, elapsed_ms) for f in frames]

    def close(self) -> None:
        self._closed = True
