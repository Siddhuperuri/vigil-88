"""Detector construction. A backend that is not implemented is a startup CapabilityError,
never a silent fallback to the null detector. Real backends are imported lazily so the base
install (no numpy, no onnxruntime) still imports and runs."""

from __future__ import annotations

from pathlib import Path

from vigil.config.schema.vision import VisionConfig
from vigil.core.clock import Clock
from vigil.core.errors import CapabilityError
from vigil.core.protocols.detector import Detector
from vigil.core.protocols.logger import LoggerLike
from vigil.observability.metrics import MetricsRegistry
from vigil.observability.probes import NvmlProbe
from vigil.vision.backends.null import NullDetector

IMPLEMENTED_BACKENDS = frozenset({"null", "onnxruntime"})
_EXTRA_HINT = "install an ONNX extra: uv sync --extra onnx-gpu (or onnx-cpu)"


def build_detector(
    cfg: VisionConfig,
    clock: Clock,
    *,
    models_dir: Path,
    logger: LoggerLike,
    metrics: MetricsRegistry | None = None,
    nvml: NvmlProbe | None = None,
) -> Detector:
    if cfg.backend == "null":
        return NullDetector(clock)
    if cfg.backend == "onnxruntime":
        try:
            from vigil.vision.backends.onnxruntime import OnnxDetector
        except ImportError as exc:
            raise CapabilityError(
                f"the onnxruntime backend needs numpy, OpenCV and onnxruntime: {_EXTRA_HINT}"
            ) from exc
        return OnnxDetector(
            cfg=cfg, models_dir=models_dir, clock=clock, logger=logger, metrics=metrics, nvml=nvml
        )
    raise CapabilityError(
        f"vision backend {cfg.backend!r} is not implemented in this build "
        f"(implemented: {', '.join(sorted(IMPLEMENTED_BACKENDS))})",
        context={"backend": cfg.backend},
    )
