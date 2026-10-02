"""Vision settings (05 §7). Selecting a backend that is not implemented is a startup
CapabilityError, never a silent fallback to the null detector."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel

DeviceName = Literal["auto", "cuda", "cpu"]
PrecisionName = Literal["auto", "fp16", "fp32"]
BackendName = Literal["null", "ultralytics", "onnxruntime", "mock"]
ModelFamily = Literal["yolox"]


class VisionConfig(ConfigModel):
    backend: BackendName = "null"
    # A model name from the registry (e.g. "yolox_s") or a file name inside paths.models_dir.
    # Never a path. The file must be listed in MANIFEST.json (see `vigil models`).
    weights: str | None = None
    model_family: ModelFamily = "yolox"

    # `auto` prefers CUDA and says why when it does not get it; `cuda` asks for it;
    # `cpu` never touches the GPU. When CUDA was wanted but fails to load or run,
    # `allow_cpu_fallback` decides between falling back (loudly) and failing.
    device: DeviceName = "auto"
    allow_cpu_fallback: bool = True
    precision: PrecisionName = "auto"

    # None = the model's native input size. These ONNX exports have a static input shape,
    # so a value other than the model's own is an error rather than a silent resize.
    input_size_px: int | None = Field(default=None, ge=32)
    resolution_tiers_px: tuple[int, ...] = (640, 512, 416)
    max_batch_size: int = Field(default=4, ge=1, le=64)
    # Hard ceiling on GPU memory the CUDA arena may take (passed to the provider), and the
    # limit the measured post-load usage is checked against.
    vram_budget_mb: int = Field(default=4000, ge=256)

    # CPU inference threads. None = onnxruntime's default (all cores), which can starve the
    # capture threads on a small machine; set it explicitly when running on CPU.
    cpu_threads: int | None = Field(default=None, ge=1)

    min_detection_confidence_ratio: float = Field(default=0.25, ge=0.0, le=1.0)
    nms_iou_ratio: float = Field(default=0.45, ge=0.0, le=1.0)
    max_detections: int = Field(default=300, ge=1)
    # model-native label -> canonical ObjectClass name. Empty means the family's built-in
    # map. Validated against ObjectClass when the detector is built (config is below domain
    # in the layering and cannot import it).
    label_map: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _tiers(self) -> Self:
        if list(self.resolution_tiers_px) != sorted(self.resolution_tiers_px, reverse=True):
            raise ValueError("resolution_tiers_px must be listed largest first")
        if self.input_size_px is not None and self.input_size_px not in self.resolution_tiers_px:
            raise ValueError("input_size_px must be one of resolution_tiers_px")
        return self

    @model_validator(mode="after")
    def _real_backend_needs_weights(self) -> Self:
        if self.backend == "onnxruntime" and not self.weights:
            raise ValueError("vision.backend 'onnxruntime' requires vision.weights (e.g. yolox_s)")
        return self
