"""Building the analysis half of the pipeline from configuration (01 §5).

`pipeline/` is where engines are constructed and wired, so this is the only place that knows
tracking, scene analysis and the event chain all exist. Keeping it out of `app.py` is not
tidiness: the application owns *lifecycle*, this file owns *composition*, and one file that did
both would have to change for either reason.

The honest-capability rule (D-008) is enforced here, once, per camera:

  * `DETECTION` comes from the detector and from nothing else;
  * `TRACKING` and `VELOCITY` are granted only when the tracking engine is enabled, because a
    module that needs identities must not run against a configuration that has none;
  * `ZONES` is granted per camera, only when that camera's zone file loaded and defined at
    least one zone. A camera with no zones does not get it, so `intrusion` is reported
    UNAVAILABLE naming the missing capability rather than running and silently finding nothing;
  * `SCENE_STABILITY` is granted only when the array backend that measures it is installed.

A camera whose zone file is broken keeps running without `ZONES` and the reason is returned for
the API to show (04 §8). An invalid zone file must not stop the application.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from vigil.config.module_params import ParamMap, load_module_params
from vigil.config.schema.camera import CameraConfig
from vigil.config.settings import Settings
from vigil.config.zones import load_zone_set
from vigil.core.capabilities import Capability, CapabilityLedger
from vigil.core.clock import Clock
from vigil.core.errors import ConfigError
from vigil.core.ids import UlidFactory
from vigil.core.protocols.logger import LoggerLike
from vigil.domain.scene import Zone
from vigil.events.binding import BindingReport
from vigil.events.registry import ModuleRegistry
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.analysis_worker import (
    AnalysisWorker,
    CameraAnalysis,
    HealthLookup,
    MuteLookup,
    TickSink,
)
from vigil.pipeline.events import EventChain, build_camera_pipeline, build_event_chain
from vigil.pipeline.queues import RoundRobinDropQueue
from vigil.pipeline.results import AnalysedFrame
from vigil.scene.analyzer import SceneAnalyzer, StabilityProbe
from vigil.tracking.bytetrack import ByteTracker

TRACKING_PROVIDER = "tracking engine (bytetrack)"
STABILITY_PROVIDER = "scene analyzer (luminance grid)"


@dataclass(frozen=True, slots=True)
class CameraZones:
    zones: tuple[Zone, ...]
    issue: str | None  # why this camera has no zones, when it was asked for some


@dataclass(frozen=True, slots=True)
class AnalysisAssembly:
    chain: EventChain
    worker: AnalysisWorker
    queue: RoundRobinDropQueue[AnalysedFrame]
    capabilities_by_camera: Mapping[str, frozenset[Capability]]
    bindings: Mapping[str, BindingReport]
    zone_issues: Mapping[str, str]

    def availability(self, camera_id: str) -> tuple[object, ...]:
        report = self.bindings.get(camera_id)
        return () if report is None else report.availability


def load_camera_zones(config_dir: Path, camera: CameraConfig, *, max_zones: int) -> CameraZones:
    """Materialise a camera's zones, or say why it has none. Never raises."""
    if camera.zones_ref is None:
        return CameraZones((), None)
    try:
        zone_set = load_zone_set(config_dir, camera.zones_ref)
    except ConfigError as exc:
        issues = "; ".join(exc.issues) if exc.issues else ""
        return CameraZones((), f"{exc.message}{f' ({issues})' if issues else ''}")
    from vigil.scene.zones import zones_from_set

    zones = zones_from_set(zone_set, camera.camera_id, max_zones=max_zones)
    if not zones:
        return CameraZones((), f"zone file {camera.zones_ref!r} defines no zones")
    return CameraZones(zones, None)


def build_stability_probe(settings: Settings) -> StabilityProbe | None:
    """The probe needs an array backend; without one, `SCENE_STABILITY` is simply not granted."""
    scene = settings.scene
    if not scene.stability_enabled:
        return None
    try:
        from vigil.scene.stability import LuminanceGridProbe
    except ImportError:
        return None
    return LuminanceGridProbe(
        columns=scene.stability_grid_columns,
        rows=scene.stability_grid_rows,
        full_change_ratio=scene.stability_full_change_ratio,
    )


def camera_capabilities(
    *,
    settings: Settings,
    detector: frozenset[Capability],
    has_zones: bool,
    has_stability: bool,
) -> frozenset[Capability]:
    granted = set(detector)
    if settings.tracking.enabled:
        granted.add(Capability.TRACKING)
        granted.add(Capability.VELOCITY)
    if has_zones:
        granted.add(Capability.ZONES)
    if has_stability:
        granted.add(Capability.SCENE_STABILITY)
    return frozenset(granted)


def _params_by_module(config_dir: Path, registry: ModuleRegistry) -> dict[str, ParamMap]:
    return {module_id: load_module_params(config_dir, module_id) for module_id in registry.ids()}


def build_analysis(
    *,
    settings: Settings,
    config_dir: Path,
    cameras: Sequence[CameraConfig],
    detector_capabilities: frozenset[Capability],
    ledger: CapabilityLedger,
    clock: Clock,
    ulids: UlidFactory,
    logger: LoggerLike,
    metrics: MetricsRegistry,
    health_for: HealthLookup,
    mutes_for: MuteLookup,
    sink: TickSink,
    registry: ModuleRegistry | None = None,
) -> AnalysisAssembly:
    chain = build_event_chain(settings=settings, logger=logger, metrics=metrics, registry=registry)
    params = _params_by_module(config_dir, chain.registry)
    probe_available = build_stability_probe(settings) is not None

    if settings.tracking.enabled:
        ledger.grant(Capability.TRACKING, TRACKING_PROVIDER)
        ledger.grant(Capability.VELOCITY, TRACKING_PROVIDER)
    if probe_available:
        ledger.grant(Capability.SCENE_STABILITY, STABILITY_PROVIDER)

    runtimes: dict[str, CameraAnalysis] = {}
    capabilities: dict[str, frozenset[Capability]] = {}
    bindings: dict[str, BindingReport] = {}
    zone_issues: dict[str, str] = {}

    for camera in cameras:
        zones = load_camera_zones(config_dir, camera, max_zones=settings.scene.max_zones_per_camera)
        if zones.issue is not None:
            zone_issues[camera.camera_id] = zones.issue
            logger.warning("camera_has_no_zones", camera_id=camera.camera_id, reason=zones.issue)
        if zones.zones:
            ledger.grant(Capability.ZONES, f"zone file {camera.zones_ref!r}")
        granted = camera_capabilities(
            settings=settings,
            detector=detector_capabilities,
            has_zones=bool(zones.zones),
            has_stability=probe_available,
        )
        capabilities[camera.camera_id] = granted
        pipeline, report = build_camera_pipeline(
            chain=chain,
            settings=settings,
            camera=camera,
            available=granted,
            clock=clock,
            ulids=ulids,
            logger=logger,
            metrics=metrics,
            params_by_module=params,
        )
        bindings[camera.camera_id] = report
        runtimes[camera.camera_id] = CameraAnalysis(
            camera_id=camera.camera_id,
            tracker=ByteTracker(
                camera_id=camera.camera_id,
                config=settings.tracking,
                logger=logger,
                metrics=metrics,
            ),
            analyzer=SceneAnalyzer(
                camera_id=camera.camera_id,
                config=settings.scene,
                zones=zones.zones,
                # One probe per camera: it holds that camera's previous frame grid.
                stability=build_stability_probe(settings),
            ),
            pipeline=pipeline,
            binding=report,
            capabilities=granted,
        )

    # No shared wake event: the analysis worker waits on this queue's own condition. Handing it
    # the inference worker's event would make every analysis hand-off spin the inference loop.
    queue: RoundRobinDropQueue[AnalysedFrame] = RoundRobinDropQueue(
        settings.pipeline.analysis_queue_size
    )
    worker = AnalysisWorker(
        cameras=runtimes,
        queue=queue,
        sink=sink,
        health_for=health_for,
        mutes_for=mutes_for,
        clock=clock,
        metrics=metrics,
        logger=logger,
        idle_wait_ms=settings.pipeline.worker_idle_wait_ms,
    )
    return AnalysisAssembly(
        chain=chain,
        worker=worker,
        queue=queue,
        capabilities_by_camera=capabilities,
        bindings=bindings,
        zone_issues=zone_issues,
    )
