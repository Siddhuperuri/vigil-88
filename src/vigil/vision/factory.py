"""Detector construction. A backend that is not implemented is a startup CapabilityError,
never a silent fallback to the null detector."""

from __future__ import annotations

from vigil.config.schema.vision import VisionConfig
from vigil.core.clock import Clock
from vigil.core.errors import CapabilityError
from vigil.core.protocols.detector import Detector
from vigil.vision.backends.null import NullDetector

IMPLEMENTED_BACKENDS = frozenset({"null"})


def build_detector(cfg: VisionConfig, clock: Clock) -> Detector:
    if cfg.backend == "null":
        return NullDetector(clock)
    raise CapabilityError(
        f"vision backend {cfg.backend!r} is not implemented in this build "
        f"(implemented: {', '.join(sorted(IMPLEMENTED_BACKENDS))}; real backends arrive in P1)",
        context={"backend": cfg.backend},
    )
