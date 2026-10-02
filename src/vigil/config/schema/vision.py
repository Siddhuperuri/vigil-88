"""Vision settings (05 §7). Backends other than `null` arrive in P1; selecting one that is
not implemented is a startup CapabilityError, not a silent fallback."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel

DeviceName = Literal["auto", "cuda", "cpu"]
PrecisionName = Literal["auto", "fp16", "fp32"]
BackendName = Literal["null", "ultralytics", "onnxruntime", "mock"]


class VisionConfig(ConfigModel):
    device: DeviceName = "auto"
    precision: PrecisionName = "auto"
    backend: BackendName = "null"
    weights: str | None = None
    input_size_px: int = Field(default=640, ge=32)
    resolution_tiers_px: tuple[int, ...] = (640, 512, 416)
    max_batch_size: int = Field(default=4, ge=1, le=64)
    vram_budget_mb: int = Field(default=4000, ge=256)
    allow_cpu_fallback: bool = True
    min_detection_confidence_ratio: float = Field(default=0.25, ge=0.0, le=1.0)
    nms_iou_ratio: float = Field(default=0.45, ge=0.0, le=1.0)
    # model-native label -> canonical ObjectClass name. Validated against ObjectClass in the
    # vision engine (config is below domain in the layering and cannot import it).
    label_map: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _tiers(self) -> Self:
        if self.input_size_px not in self.resolution_tiers_px:
            raise ValueError("input_size_px must be one of resolution_tiers_px")
        if list(self.resolution_tiers_px) != sorted(self.resolution_tiers_px, reverse=True):
            raise ValueError("resolution_tiers_px must be listed largest first")
        return self
