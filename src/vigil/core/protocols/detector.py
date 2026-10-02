"""Detector interface (04 §3.1)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.core.capabilities import Capability
    from vigil.domain.detection import DetectionSet, ModelDescriptor, PreparedFrame


class Detector(Protocol):
    """`detect_batch` is the only inference entry point: a single frame is a batch of one.

    Returned boxes are in source-frame pixel space. `provides` states which capabilities
    this detector can honestly supply. A detector that produces no detections must not
    claim DETECTION.
    """

    @property
    def descriptor(self) -> ModelDescriptor: ...

    @property
    def provides(self) -> frozenset[Capability]: ...

    def warmup(self) -> None: ...

    def detect_batch(self, frames: Sequence[PreparedFrame]) -> Sequence[DetectionSet]:
        """Returns exactly one DetectionSet per input frame, in order. Raises InferenceError."""
        ...

    def close(self) -> None:
        """Idempotent."""
        ...
