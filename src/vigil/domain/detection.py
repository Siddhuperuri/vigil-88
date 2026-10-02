"""Detections and model identity (05 §4, 04 §3.1).

Boxes are ALWAYS in source-frame pixel space, never letterboxed model space.
"""

from __future__ import annotations

from dataclasses import dataclass

from vigil.core.geometry import BBox
from vigil.domain._validate import (
    require_non_empty,
    require_non_negative,
    require_positive,
    require_ratio,
)
from vigil.domain.enums import ObjectClass
from vigil.domain.frame import Frame, FrameMeta


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """Copied onto every Incident so a past decision stays explainable (04 §3.1)."""

    name: str
    version: str
    backend: str
    weights_sha256: str | None
    input_size_px: int | None
    precision: str
    device: str
    license: str | None

    def __post_init__(self) -> None:
        require_non_empty("ModelDescriptor.name", self.name)
        require_non_empty("ModelDescriptor.backend", self.backend)
        if self.input_size_px is not None:
            require_positive("input_size_px", self.input_size_px)


@dataclass(frozen=True, slots=True)
class Detection:
    bbox: BBox
    object_class: ObjectClass
    confidence_ratio: float
    native_label: str

    def __post_init__(self) -> None:
        require_ratio("Detection.confidence_ratio", self.confidence_ratio)


@dataclass(frozen=True, slots=True)
class DetectionSet:
    frame_meta: FrameMeta
    detections: tuple[Detection, ...]
    model: ModelDescriptor
    inference_latency_ms: float

    def __post_init__(self) -> None:
        require_non_negative("inference_latency_ms", self.inference_latency_ms)
        object.__setattr__(self, "detections", tuple(self.detections))


@dataclass(frozen=True, slots=True)
class PreparedFrame:
    """A frame handed to a detector. Extended with the letterbox transform in P1."""

    frame: Frame
