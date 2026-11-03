"""What a detector factory is handed: everything it may need, as one value, so a factory
(production or test) never needs the application itself."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vigil.config.paths import ResolvedPaths
from vigil.config.settings import Settings
from vigil.core.clock import Clock
from vigil.core.protocols.detector import Detector
from vigil.core.protocols.logger import LoggerLike
from vigil.observability.metrics import MetricsRegistry
from vigil.observability.probes import NvmlProbe
from vigil.vision.factory import build_detector


@dataclass(frozen=True, slots=True)
class DetectorContext:
    """Everything a detector may need from the application."""

    settings: Settings
    paths: ResolvedPaths
    clock: Clock
    metrics: MetricsRegistry
    logger: LoggerLike
    nvml: NvmlProbe | None


DetectorFactory = Callable[[DetectorContext], Detector]


def default_detector_factory(ctx: DetectorContext) -> Detector:
    return build_detector(
        ctx.settings.vision,
        ctx.clock,
        models_dir=ctx.paths.models_dir,
        logger=ctx.logger,
        metrics=ctx.metrics,
        nvml=ctx.nvml,
    )
