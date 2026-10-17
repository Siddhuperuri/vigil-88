"""Module discovery, enablement and capability gating (06 §5, D-008).

Three separate questions, deliberately not collapsed into one:

  1. **Discovery** — what modules exist in this build. Scanning `events/builtin/` plus the
     `vigil88.modules` entry-point group.
  2. **Enablement** — which of them the operator asked for, via `modules.enabled`. Discovery is
     never enablement: a module dropped into the tree must not start making decisions.
  3. **Availability** — whether an enabled module can actually do its job *on this camera*,
     given the capabilities the running configuration supplies.

The third is the anti-fake-functionality mechanism (04 §4). A module whose required capability
is missing is UNAVAILABLE: it does not run, it is not offered in the UI, and the reason names
the capability and what would provide it. Nothing is operational merely because its Python
class exists.
"""

from __future__ import annotations

import pkgutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import EntryPoint, entry_points
from types import ModuleType

from vigil.core.capabilities import (
    Availability,
    AvailabilityReport,
    Capability,
    resolve_availability,
)
from vigil.core.errors import ModuleError
from vigil.core.protocols.module import DetectionModule, ModuleDescriptor
from vigil.domain.enums import EventType

ENTRY_POINT_GROUP = "vigil88.modules"
BUILTIN_PACKAGE = "vigil.events.builtin"
DESCRIPTOR_ATTR = "MODULE"
FACTORY_ATTR = "create"

ModuleFactory = Callable[[], DetectionModule]


@dataclass(frozen=True, slots=True)
class ModuleRegistration:
    descriptor: ModuleDescriptor
    factory: ModuleFactory
    source: str  # "builtin:intrusion" or "entry_point:some-package"

    @property
    def module_id(self) -> str:
        return self.descriptor.module_id


@dataclass(frozen=True, slots=True)
class ModuleAvailability:
    """What the API and the console render. They consume this; they never decide it themselves."""

    module_id: str
    event_type: EventType
    version: str
    camera_id: str
    enabled: bool
    status: Availability
    requires: frozenset[Capability]
    optional: frozenset[Capability]
    missing_required: frozenset[Capability]
    missing_optional: frozenset[Capability]
    reason: str | None
    description: str

    @property
    def will_run(self) -> bool:
        """Enabled, and its required capabilities are all present. The only thing that matters."""
        return self.enabled and self.status is not Availability.UNAVAILABLE

    @property
    def caveats(self) -> tuple[str, ...]:
        """Quality caveats to copy onto every event this module produces while DEGRADED, so the
        limitation travels with the data (04 §4)."""
        if self.status is not Availability.DEGRADED:
            return ()
        return tuple(
            f"{self.module_id}: running without {c.value}" for c in sorted(self.missing_optional)
        )


def _read_module_package(package: ModuleType, source: str) -> ModuleRegistration:
    """A module package must export a `MODULE` descriptor and a `create()` factory.

    Both are required rather than inferred, so a half-written package fails loudly at startup
    instead of being discovered and silently skipped.
    """
    descriptor = getattr(package, DESCRIPTOR_ATTR, None)
    factory = getattr(package, FACTORY_ATTR, None)
    if descriptor is None or not isinstance(descriptor, ModuleDescriptor):
        raise ModuleError(
            f"{source} does not export a {DESCRIPTOR_ATTR} descriptor",
            context={"source": source},
        )
    if factory is None or not callable(factory):
        raise ModuleError(
            f"{source} does not export a callable {FACTORY_ATTR}()",
            context={"source": source, "module_id": descriptor.module_id},
        )
    if descriptor.requires & descriptor.optional:
        raise ModuleError(
            f"{source} declares the same capability as both required and optional",
            context={"overlap": sorted(c.value for c in descriptor.requires & descriptor.optional)},
        )
    return ModuleRegistration(descriptor=descriptor, factory=factory, source=source)


class ModuleRegistry:
    """Deterministic lookup over discovered modules. Holds no camera state and no UI concerns."""

    __slots__ = ("_registrations",)

    def __init__(self) -> None:
        self._registrations: dict[str, ModuleRegistration] = {}

    def __len__(self) -> int:
        return len(self._registrations)

    def register(self, registration: ModuleRegistration) -> None:
        module_id = registration.module_id
        existing = self._registrations.get(module_id)
        if existing is not None:
            raise ModuleError(
                f"two modules claim the id {module_id!r}",
                context={"first": existing.source, "second": registration.source},
            )
        self._registrations[module_id] = registration

    def ids(self) -> tuple[str, ...]:
        """Sorted, so iteration order never depends on filesystem or entry-point order."""
        return tuple(sorted(self._registrations))

    def descriptors(self) -> tuple[ModuleDescriptor, ...]:
        return tuple(self._registrations[mid].descriptor for mid in self.ids())

    def get(self, module_id: str) -> ModuleRegistration:
        try:
            return self._registrations[module_id]
        except KeyError:
            known = ", ".join(self.ids()) or "(none)"
            raise ModuleError(
                f"unknown module {module_id!r}; discovered modules: {known}",
                context={"module_id": module_id},
            ) from None

    def has(self, module_id: str) -> bool:
        return module_id in self._registrations

    # ---------------------------------------------------------------- gating

    def availability(
        self,
        module_id: str,
        *,
        camera_id: str,
        available: frozenset[Capability],
        enabled_ids: Iterable[str],
    ) -> ModuleAvailability:
        registration = self.get(module_id)
        descriptor = registration.descriptor
        enabled = module_id in set(enabled_ids)
        report: AvailabilityReport = resolve_availability(
            descriptor.requires, descriptor.optional, available
        )
        reason = report.reason
        if not enabled:
            reason = f"not listed in modules.enabled{'' if reason is None else f'; {reason}'}"
        return ModuleAvailability(
            module_id=module_id,
            event_type=descriptor.event_type,
            version=descriptor.version,
            camera_id=camera_id,
            enabled=enabled,
            status=report.status,
            requires=descriptor.requires,
            optional=descriptor.optional,
            missing_required=report.missing_required,
            missing_optional=report.missing_optional,
            reason=reason,
            description=descriptor.description,
        )

    def table(
        self, *, camera_id: str, available: frozenset[Capability], enabled_ids: Iterable[str]
    ) -> tuple[ModuleAvailability, ...]:
        """Every discovered module's standing on one camera, in deterministic order."""
        enabled = tuple(enabled_ids)
        return tuple(
            self.availability(mid, camera_id=camera_id, available=available, enabled_ids=enabled)
            for mid in self.ids()
        )

    def unknown_enabled(self, enabled_ids: Iterable[str]) -> tuple[str, ...]:
        """Enabled ids that match no discovered module. A typo in `modules.enabled` must be an
        error at startup, not a module that mysteriously never fires."""
        return tuple(sorted(mid for mid in enabled_ids if mid not in self._registrations))


# ---------------------------------------------------------------- discovery


def discover_builtin(package_name: str = BUILTIN_PACKAGE) -> tuple[ModuleRegistration, ...]:
    """Every sub-package of `events/builtin/` that exports a descriptor and a factory."""
    package = import_module(package_name)
    paths = list(getattr(package, "__path__", ()))
    found: list[ModuleRegistration] = []
    for info in sorted(pkgutil.iter_modules(paths), key=lambda i: i.name):
        if not info.ispkg or info.name.startswith("_"):
            continue
        sub = import_module(f"{package_name}.{info.name}")
        found.append(_read_module_package(sub, f"builtin:{info.name}"))
    return tuple(found)


def discover_entry_points(group: str = ENTRY_POINT_GROUP) -> tuple[ModuleRegistration, ...]:
    """Third-party modules registered under the `vigil88.modules` group.

    A broken third-party package is reported as a `ModuleError` naming it, never swallowed: a
    plugin that fails to load silently is indistinguishable from one that is not installed.
    """
    found: list[ModuleRegistration] = []
    selected: Sequence[EntryPoint] = tuple(entry_points(group=group))
    for entry in sorted(selected, key=lambda e: e.name):
        try:
            loaded = entry.load()
        except (ImportError, AttributeError) as exc:
            raise ModuleError(
                f"entry point {entry.name!r} in group {group} failed to load",
                context={"entry_point": entry.name, "error": str(exc)},
            ) from exc
        if not isinstance(loaded, ModuleType):
            raise ModuleError(
                f"entry point {entry.name!r} must point at a module package exporting "
                f"{DESCRIPTOR_ATTR} and {FACTORY_ATTR}()",
                context={"entry_point": entry.name},
            )
        found.append(_read_module_package(loaded, f"entry_point:{entry.name}"))
    return tuple(found)


def build_registry(
    *,
    include_builtin: bool = True,
    include_entry_points: bool = True,
    extra: Iterable[ModuleRegistration] = (),
) -> ModuleRegistry:
    registry = ModuleRegistry()
    if include_builtin:
        for registration in discover_builtin():
            registry.register(registration)
    if include_entry_points:
        for registration in discover_entry_points():
            registry.register(registration)
    for registration in extra:
        registry.register(registration)
    return registry


def descriptor_params(
    descriptor: ModuleDescriptor, *overrides: Mapping[str, object]
) -> dict[str, object]:
    """Descriptor defaults, then each override layer in order (06 §5)."""
    merged: dict[str, object] = dict(descriptor.default_params)
    for layer in overrides:
        merged.update(layer)
    return merged
