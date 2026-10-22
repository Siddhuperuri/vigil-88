"""Building the analysis and response stages together, in the order their inputs allow.

There is one genuine ordering problem here and it is worth naming, because the obvious
resolutions are both wrong.

The analysis worker needs somewhere to send a decided tick, and that somewhere is the response
stack's queue. The response stack needs the per-camera module versions and capability caveats
that are only known once modules have been bound -- which happens while the analysis stage is
built. Each needs something from the other.

Building the response stack first and querying the registry separately would mean two different
answers to "which modules are bound on this camera", and the one the incident records could
disagree with the one the console shows. Passing the application in would make both stages
depend on lifecycle. So instead the hand-off is made explicit: analysis sends to a `TickRouter`
whose target is set once the response stack exists. Until then the router counts and drops,
which is exactly what should happen to a decided event with nowhere to be stored -- visibly,
not silently.

`storage.enabled = false` is a supported configuration: the analysis stage still runs and the
router stays unattached, so the application can report that incidents are not being persisted
rather than appearing to persist them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from vigil.config.paths import ResolvedPaths
from vigil.config.schema.camera import CameraConfig
from vigil.config.settings import Settings
from vigil.core.capabilities import Capability, CapabilityLedger
from vigil.core.clock import Clock
from vigil.core.ids import UlidFactory
from vigil.core.protocols.logger import LoggerLike
from vigil.domain.detection import ModelDescriptor
from vigil.domain.incident import SystemSnapshot
from vigil.domain.mute import Mute
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.analysis import AnalysisTick
from vigil.pipeline.analysis_worker import HealthLookup
from vigil.pipeline.assembly import AnalysisAssembly, build_analysis
from vigil.pipeline.response_stack import ResponseStack, build_response_stack
from vigil.pipeline.response_worker import ResponseIdentity
from vigil.pipeline.results import LatestDetections


@dataclass(slots=True)
class TickRouter:
    """A one-slot indirection between the analysis and response stages.

    Dropped ticks are counted rather than ignored: "the response stage was not built" and "the
    response stage lost an event" must not look the same from the outside.
    """

    target: Callable[[AnalysisTick], None] | None = None
    dropped: int = 0

    def send(self, tick: AnalysisTick) -> None:
        if self.target is None:
            self.dropped += 1
            return
        self.target(tick)

    def attach(self, target: Callable[[AnalysisTick], None]) -> None:
        self.target = target

    @property
    def attached(self) -> bool:
        return self.target is not None


@dataclass(frozen=True, slots=True)
class Stages:
    analysis: AnalysisAssembly
    response: ResponseStack | None
    router: TickRouter
    capabilities_by_camera: Mapping[str, frozenset[Capability]] = field(default_factory=dict)

    @property
    def persists_incidents(self) -> bool:
        return self.response is not None


def module_versions(analysis: AnalysisAssembly) -> dict[str, str]:
    """The bound module versions, from the same reports the console renders."""
    versions: dict[str, str] = {}
    for report in analysis.bindings.values():
        for runtime in report.runtimes:
            versions[runtime.module_id] = runtime.descriptor.version
    return versions


def caveats_by_camera(analysis: AnalysisAssembly) -> dict[str, tuple[str, ...]]:
    """Quality caveats from every DEGRADED module that will actually run, per camera.

    Copied onto each incident so the limitation travels with the data (04 §4): an incident
    produced without velocity should say so wherever it is read.
    """
    return {camera_id: report.caveats for camera_id, report in analysis.bindings.items()}


def build_stages(
    *,
    settings: Settings,
    paths: ResolvedPaths,
    cameras: Sequence[CameraConfig],
    detector_capabilities: frozenset[Capability],
    detector: Callable[[], ModelDescriptor | None],
    system: Callable[[], SystemSnapshot],
    results: LatestDetections,
    ledger: CapabilityLedger,
    clock: Clock,
    ulids: UlidFactory,
    logger: LoggerLike,
    metrics: MetricsRegistry,
    health_for: HealthLookup,
) -> Stages:
    router = TickRouter()
    mutes_holder: list[Callable[[str], Sequence[Mute]]] = [lambda _camera_id: ()]

    analysis = build_analysis(
        settings=settings,
        config_dir=paths.config_dir,
        cameras=cameras,
        detector_capabilities=detector_capabilities,
        ledger=ledger,
        clock=clock,
        ulids=ulids,
        logger=logger,
        metrics=metrics,
        health_for=health_for,
        # Mutes are persisted, so the lookup only exists once storage does. Until then no mute
        # can be in force, which is the truthful answer rather than a convenient one.
        mutes_for=lambda camera_id: mutes_holder[0](camera_id),
        sink=router.send,
    )

    response: ResponseStack | None = None
    if settings.storage.enabled:
        response = build_response_stack(
            settings=settings,
            paths=paths,
            cameras=cameras,
            results=results,
            identity=ResponseIdentity(
                module_versions=module_versions(analysis),
                locations={c.camera_id: c.location for c in cameras},
                caveats=caveats_by_camera(analysis),
                model=detector,
                system=system,
            ),
            clock=clock,
            ulids=ulids,
            logger=logger,
            metrics=metrics,
        )
        router.attach(response.enqueue)
        mutes_holder[0] = response.mutes.mutes_for
    else:
        logger.warning(
            "incident_persistence_disabled",
            reason="storage.enabled is false: events are analysed but no incident is stored",
        )

    return Stages(
        analysis=analysis,
        response=response,
        router=router,
        capabilities_by_camera=analysis.capabilities_by_camera,
    )
