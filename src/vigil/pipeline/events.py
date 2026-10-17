"""Assembling the event chain from configuration (01 §5: `pipeline/` constructs the engines).

One function builds a camera's `AnalysisPipeline`, and the application, the CLI and the tests
all call it. That matters more than it looks: a test that assembled the chain differently from
production would prove something about the test's wiring rather than about the system.

The suppression ledger and the verification engine are shared across cameras, because dedup,
cooldown and rate limiting are cross-camera concerns in their bookkeeping even though every
decision is per camera. The accumulator store and module runtimes are per camera, because that
state is what one thread owns (D-005).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from vigil.config.module_params import ParamMap
from vigil.config.schema.camera import CameraConfig
from vigil.config.settings import Settings
from vigil.core.capabilities import Capability
from vigil.core.clock import Clock
from vigil.core.errors import ConfigError
from vigil.core.ids import UlidFactory
from vigil.core.protocols.logger import LoggerLike
from vigil.events.binding import BindingReport, bind_modules
from vigil.events.engine import EventEngine
from vigil.events.registry import ModuleRegistry, build_registry
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.analysis import AnalysisPipeline
from vigil.severity.engine import SeverityScorer
from vigil.temporal.store import AccumulatorStore, slot_ms_for
from vigil.verification.engine import VerificationEngine
from vigil.verification.ledger import SuppressionLedger


@dataclass(frozen=True, slots=True)
class EventChain:
    """The shared half of the chain, built once per process."""

    registry: ModuleRegistry
    ledger: SuppressionLedger
    verification: VerificationEngine
    scorer: SeverityScorer


def build_event_chain(
    *,
    settings: Settings,
    logger: LoggerLike,
    metrics: MetricsRegistry,
    registry: ModuleRegistry | None = None,
) -> EventChain:
    """Build the process-wide half. Rejects an unknown module id rather than ignoring it."""
    resolved = registry if registry is not None else build_registry()
    unknown = resolved.unknown_enabled(settings.modules.enabled)
    if unknown:
        known = ", ".join(resolved.ids()) or "(none)"
        raise ConfigError(
            "modules.enabled names modules that do not exist",
            issues=[f"unknown: {', '.join(unknown)}", f"discovered: {known}"],
        )
    ledger = SuppressionLedger(settings.verification)
    return EventChain(
        registry=resolved,
        ledger=ledger,
        verification=VerificationEngine(
            ledger=ledger,
            config=settings.verification,
            logger=logger,
            metrics=metrics,
        ),
        scorer=SeverityScorer(config=settings.severity, logger=logger),
    )


def build_camera_pipeline(
    *,
    chain: EventChain,
    settings: Settings,
    camera: CameraConfig,
    available: frozenset[Capability],
    clock: Clock,
    ulids: UlidFactory,
    logger: LoggerLike,
    metrics: MetricsRegistry,
    params_by_module: Mapping[str, ParamMap] | None = None,
) -> tuple[AnalysisPipeline, BindingReport]:
    """Build one camera's half, and report honestly what it can and cannot do.

    The report is returned rather than logged and discarded: the API and console render module
    availability from it, and a module that is UNAVAILABLE must be visibly so (04 §4).
    """
    report = bind_modules(
        registry=chain.registry,
        camera_id=camera.camera_id,
        available=available,
        config=settings.modules,
        logger=logger,
        params_by_module=params_by_module or {},
        camera=camera,
    )
    events = EventEngine(
        camera_id=camera.camera_id,
        runtimes=report.runtimes,
        config=settings.modules,
        clock=clock,
        logger=logger,
        metrics=metrics,
    )
    store = AccumulatorStore(
        camera_id=camera.camera_id,
        config=settings.temporal,
        support_slot_ms=slot_ms_for(settings.temporal, camera.target_inference_fps),
    )
    pipeline = AnalysisPipeline(
        camera_id=camera.camera_id,
        events=events,
        store=store,
        verification=chain.verification,
        ledger=chain.ledger,
        scorer=chain.scorer,
        temporal_config=settings.temporal,
        verification_config=settings.verification,
        arbitration_config=settings.arbitration,
        ulids=ulids,
        logger=logger,
        metrics=metrics,
        camera_trust_ratio=camera.trust_ratio,
    )
    return (pipeline, report)


def module_availability_table(
    *,
    chain: EventChain,
    settings: Settings,
    cameras: Sequence[CameraConfig],
    capabilities_by_camera: Mapping[str, frozenset[Capability]],
) -> dict[str, tuple[object, ...]]:
    """What the API serves for "which events work on which camera, and why not".

    The UI consumes this; it never works out availability for itself (§17).
    """
    return {
        camera.camera_id: chain.registry.table(
            camera_id=camera.camera_id,
            available=capabilities_by_camera.get(camera.camera_id, frozenset()),
            enabled_ids=settings.modules.enabled,
        )
        for camera in cameras
    }
