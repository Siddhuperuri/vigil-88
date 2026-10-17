"""Turning a registry plus a camera's configuration into bound, runnable modules (06 §5).

Four things happen here, in this order, and the order matters:

  1. **Enablement** is checked against `modules.enabled`. A discovered module is not an enabled
     one.
  2. **Capability gating** decides availability for *this* camera (D-008). An UNAVAILABLE module
     is never constructed, never bound, and never run — it is reported, with the missing
     capability and what would supply it.
  3. **Parameters** are merged across the three levels and handed to the module.
  4. **Binding** runs the module's own validation. An unknown parameter key fails here, at
     startup, which is the whole point of validating against the module's model: a
     silently-ignored typo in a threshold is indistinguishable from the module not working.

A module that fails to bind is reported as unavailable with the validation error as its reason,
and the camera starts without it. One bad module must not stop a camera.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from vigil.config.module_params import ParamMap
from vigil.config.schema.camera import CameraConfig
from vigil.config.schema.modules import ModulesConfig
from vigil.core.capabilities import Availability, Capability
from vigil.core.protocols.logger import LoggerLike
from vigil.core.protocols.module import ModuleContext
from vigil.events.engine import ModuleRuntime
from vigil.events.registry import ModuleAvailability, ModuleRegistry, descriptor_params

_NO_PARAMS: Mapping[str, ParamMap] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class BindingReport:
    """Everything the API and console need to explain a camera's module set honestly."""

    camera_id: str
    runtimes: tuple[ModuleRuntime, ...]
    availability: tuple[ModuleAvailability, ...]
    bind_failures: Mapping[str, str]

    @property
    def runnable_ids(self) -> tuple[str, ...]:
        return tuple(r.module_id for r in self.runtimes)

    @property
    def unavailable(self) -> tuple[ModuleAvailability, ...]:
        return tuple(a for a in self.availability if a.status is Availability.UNAVAILABLE)

    @property
    def caveats(self) -> tuple[str, ...]:
        """Caveats from every DEGRADED module that will actually run, for copying onto events."""
        return tuple(c for a in self.availability if a.will_run for c in a.caveats)


def resolve_params(
    registry: ModuleRegistry,
    module_id: str,
    *,
    file_params: ParamMap,
    camera: CameraConfig | None,
) -> dict[str, object]:
    """Descriptor defaults, then `config/modules/<id>.yaml`, then this camera's overrides."""
    descriptor = registry.get(module_id).descriptor
    camera_params: ParamMap = {} if camera is None else camera.module_params.get(module_id, {})
    return descriptor_params(descriptor, file_params, camera_params)


def bind_modules(
    *,
    registry: ModuleRegistry,
    camera_id: str,
    available: frozenset[Capability],
    config: ModulesConfig,
    logger: LoggerLike,
    params_by_module: Mapping[str, ParamMap] = _NO_PARAMS,
    camera: CameraConfig | None = None,
) -> BindingReport:
    """Build the runtimes for one camera. Never raises for a single bad module."""
    log = logger.bind(camera_id=camera_id)
    table = registry.table(camera_id=camera_id, available=available, enabled_ids=config.enabled)
    runtimes: list[ModuleRuntime] = []
    failures: dict[str, str] = {}
    final: list[ModuleAvailability] = []

    for entry in table:
        if not entry.will_run:
            final.append(entry)
            if entry.enabled:
                log.info(
                    "module_unavailable",
                    module_id=entry.module_id,
                    missing=sorted(c.value for c in entry.missing_required),
                    reason=entry.reason,
                )
            continue
        params = resolve_params(
            registry,
            entry.module_id,
            file_params=params_by_module.get(entry.module_id, {}),
            camera=camera,
        )
        runtime = _bind_one(registry, entry, params=params, camera_id=camera_id, logger=log)
        if isinstance(runtime, str):
            failures[entry.module_id] = runtime
            final.append(_as_unavailable(entry, runtime))
            log.error("module_bind_failed", module_id=entry.module_id, reason=runtime)
            continue
        runtimes.append(runtime)
        final.append(entry)
        if entry.status is Availability.DEGRADED:
            log.warning(
                "module_degraded",
                module_id=entry.module_id,
                missing_optional=sorted(c.value for c in entry.missing_optional),
            )

    return BindingReport(
        camera_id=camera_id,
        runtimes=tuple(runtimes),
        availability=tuple(final),
        bind_failures=dict(failures),
    )


def _bind_one(
    registry: ModuleRegistry,
    entry: ModuleAvailability,
    *,
    params: Mapping[str, object],
    camera_id: str,
    logger: LoggerLike,
) -> ModuleRuntime | str:
    """Construct and bind one module. Returns the runtime, or a reason string on failure."""
    registration = registry.get(entry.module_id)
    context = ModuleContext(
        camera_id=camera_id,
        params=params,
        logger=logger.bind(module_id=entry.module_id),
    )
    try:
        module = registration.factory()
        module.bind(context)
    except Exception as exc:  # noqa: BLE001 - a plugin's own validation may raise anything
        return f"{type(exc).__name__}: {exc}"
    return ModuleRuntime(
        descriptor=registration.descriptor,
        module=module,
        availability=entry,
        params=dict(params),
    )


def _as_unavailable(entry: ModuleAvailability, reason: str) -> ModuleAvailability:
    """A module that could not be bound is unavailable, with the validation error as the reason.
    It must not appear operational because its capabilities happened to be satisfied."""
    return ModuleAvailability(
        module_id=entry.module_id,
        event_type=entry.event_type,
        version=entry.version,
        camera_id=entry.camera_id,
        enabled=entry.enabled,
        status=Availability.UNAVAILABLE,
        requires=entry.requires,
        optional=entry.optional,
        missing_required=entry.missing_required,
        missing_optional=entry.missing_optional,
        reason=f"failed to bind: {reason}",
        description=entry.description,
    )


def required_capabilities(
    registry: ModuleRegistry, enabled: Sequence[str]
) -> frozenset[Capability]:
    """Every capability the enabled set needs. Lets `vigil doctor` say what is missing overall."""
    needed: set[Capability] = set()
    for module_id in enabled:
        if registry.has(module_id):
            needed |= registry.get(module_id).descriptor.requires
    return frozenset(needed)
