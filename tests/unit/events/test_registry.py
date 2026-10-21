"""Discovery, enablement and capability gating as three separate questions (06 §5, D-008)."""

from __future__ import annotations

from types import ModuleType, SimpleNamespace

import pytest

from vigil.core.capabilities import Availability, Capability
from vigil.core.errors import ModuleError
from vigil.core.protocols.module import ModuleDescriptor
from vigil.domain.enums import EventType
from vigil.events.registry import (
    ModuleRegistration,
    ModuleRegistry,
    _read_module_package,
    build_registry,
    descriptor_params,
    discover_builtin,
    discover_entry_points,
)

INTRUSION_CAPS = frozenset({Capability.DETECTION, Capability.TRACKING, Capability.ZONES})
FULL = INTRUSION_CAPS | {Capability.VELOCITY}


def descriptor(
    module_id: str = "demo",
    *,
    requires: frozenset[Capability] = frozenset({Capability.DETECTION}),
    optional: frozenset[Capability] = frozenset(),
    params: dict[str, object] | None = None,
) -> ModuleDescriptor:
    return ModuleDescriptor(
        module_id=module_id,
        event_type=EventType.INTRUSION,
        version="1.0.0",
        requires=requires,
        optional=optional,
        default_params=params or {},
        description=f"{module_id} for tests",
    )


def registration(desc: ModuleDescriptor, source: str = "test") -> ModuleRegistration:
    return ModuleRegistration(descriptor=desc, factory=lambda: SimpleNamespace(), source=source)  # type: ignore[arg-type,return-value]


def fake_package(name: str, **attrs: object) -> ModuleType:
    package = ModuleType(name)
    for key, value in attrs.items():
        setattr(package, key, value)
    return package


# ------------------------------------------------------------------ discovery


# Every module in `events/builtin/`, in discovery order. `fire` and `smoke` are
# declaration-only (ADR 0015): discovered and reported, never runnable.
SHIPPED = ("abandoned_object", "fire", "intrusion", "person_down", "smoke")


def test_every_builtin_module_is_discovered() -> None:
    found = discover_builtin()
    assert [r.module_id for r in found] == list(SHIPPED)
    assert {r.source for r in found} == {f"builtin:{mid}" for mid in SHIPPED}


def test_discovery_is_deterministic() -> None:
    assert build_registry(include_entry_points=False).ids() == SHIPPED


def test_a_package_without_a_descriptor_is_a_loud_failure() -> None:
    with pytest.raises(ModuleError, match="does not export a MODULE"):
        _read_module_package(fake_package("broken", create=lambda: None), "builtin:broken")


def test_a_package_without_a_factory_is_a_loud_failure() -> None:
    with pytest.raises(ModuleError, match="callable create"):
        _read_module_package(fake_package("broken", MODULE=descriptor()), "builtin:broken")


def test_a_descriptor_that_is_not_a_descriptor_is_rejected() -> None:
    with pytest.raises(ModuleError, match="does not export a MODULE"):
        _read_module_package(
            fake_package("broken", MODULE={"module_id": "x"}, create=lambda: None),
            "builtin:broken",
        )


def test_a_capability_declared_both_required_and_optional_is_rejected() -> None:
    """Over- and under-declaration are the two ways capability negotiation becomes theatre."""
    bad = descriptor(
        requires=frozenset({Capability.VELOCITY}), optional=frozenset({Capability.VELOCITY})
    )
    with pytest.raises(ModuleError, match="both required and optional"):
        _read_module_package(fake_package("b", MODULE=bad, create=lambda: None), "builtin:b")


def test_two_modules_claiming_one_id_is_an_error() -> None:
    registry = ModuleRegistry()
    registry.register(registration(descriptor("dup"), "builtin:a"))
    with pytest.raises(ModuleError, match="two modules claim the id"):
        registry.register(registration(descriptor("dup"), "entry_point:b"))


def test_an_unknown_module_id_names_what_was_discovered() -> None:
    registry = ModuleRegistry()
    registry.register(registration(descriptor("real")))
    with pytest.raises(ModuleError, match="unknown module 'ghost'; discovered modules: real"):
        registry.get("ghost")


# ------------------------------------------------------------------ enablement is not discovery


def test_a_discovered_module_that_is_not_enabled_will_not_run() -> None:
    registry = build_registry(include_entry_points=False)
    entry = registry.availability("intrusion", camera_id="cam-01", available=FULL, enabled_ids=())
    assert entry.status is Availability.ACTIVE  # its capabilities are satisfied...
    assert not entry.enabled  # ...but nobody asked for it
    assert not entry.will_run
    assert entry.reason is not None and "modules.enabled" in entry.reason


def test_an_enabled_module_with_every_capability_is_active() -> None:
    registry = build_registry(include_entry_points=False)
    entry = registry.availability(
        "intrusion", camera_id="cam-01", available=FULL, enabled_ids=("intrusion",)
    )
    assert entry.status is Availability.ACTIVE
    assert entry.will_run
    assert entry.reason is None
    assert entry.caveats == ()


def test_a_typo_in_modules_enabled_is_detectable() -> None:
    """A module that mysteriously never fires is worse than a startup error."""
    registry = build_registry(include_entry_points=False)
    assert registry.unknown_enabled(("intrusion", "intrusionn")) == ("intrusionn",)
    assert registry.unknown_enabled(("intrusion",)) == ()


# ------------------------------------------------------------------ capability gating (D-008)


@pytest.mark.parametrize("missing", sorted(INTRUSION_CAPS))
def test_a_module_missing_a_required_capability_is_unavailable(missing: Capability) -> None:
    registry = build_registry(include_entry_points=False)
    entry = registry.availability(
        "intrusion",
        camera_id="cam-01",
        available=FULL - {missing},
        enabled_ids=("intrusion",),
    )
    assert entry.status is Availability.UNAVAILABLE
    assert not entry.will_run
    assert entry.missing_required == frozenset({missing})


def test_an_unavailable_module_s_reason_names_the_fix() -> None:
    registry = build_registry(include_entry_points=False)
    entry = registry.availability(
        "intrusion",
        camera_id="cam-01",
        available=frozenset({Capability.DETECTION, Capability.TRACKING}),
        enabled_ids=("intrusion",),
    )
    assert entry.reason is not None
    assert "zones" in entry.reason
    # The reason has to be actionable, not merely accurate: it names the config key and the
    # file an operator has to create (04 §4).
    assert "zones_ref" in entry.reason
    assert "config/zones/" in entry.reason


def test_a_module_missing_only_an_optional_capability_is_degraded() -> None:
    registry = build_registry(include_entry_points=False)
    entry = registry.availability(
        "intrusion", camera_id="cam-01", available=INTRUSION_CAPS, enabled_ids=("intrusion",)
    )
    assert entry.status is Availability.DEGRADED
    assert entry.will_run  # degraded still works
    assert entry.missing_optional == frozenset({Capability.VELOCITY})


def test_a_degraded_module_carries_caveats_for_its_events() -> None:
    """The quality caveat must travel with the data (04 §4)."""
    registry = build_registry(include_entry_points=False)
    entry = registry.availability(
        "intrusion", camera_id="cam-01", available=INTRUSION_CAPS, enabled_ids=("intrusion",)
    )
    assert entry.caveats == ("intrusion: running without velocity",)


def test_availability_is_per_camera() -> None:
    registry = build_registry(include_entry_points=False)
    calibrated = registry.availability(
        "intrusion", camera_id="cam-01", available=FULL, enabled_ids=("intrusion",)
    )
    bare = registry.availability(
        "intrusion",
        camera_id="cam-02",
        available=frozenset({Capability.DETECTION}),
        enabled_ids=("intrusion",),
    )
    assert calibrated.camera_id == "cam-01" and calibrated.will_run
    assert bare.camera_id == "cam-02" and not bare.will_run


def test_the_table_covers_every_discovered_module_in_order() -> None:
    registry = ModuleRegistry()
    for mid in ("zebra", "alpha", "mid"):
        registry.register(registration(descriptor(mid)))
    table = registry.table(
        camera_id="cam-01", available=frozenset({Capability.DETECTION}), enabled_ids=("mid",)
    )
    assert [e.module_id for e in table] == ["alpha", "mid", "zebra"]
    assert [e.will_run for e in table] == [False, True, False]


def test_the_table_exposes_metadata_the_ui_needs() -> None:
    registry = build_registry(include_entry_points=False)
    table = registry.table(camera_id="cam-01", available=FULL, enabled_ids=("intrusion",))
    entry = next(e for e in table if e.module_id == "intrusion")
    assert entry.event_type is EventType.INTRUSION
    assert entry.version == "1.0.0"
    assert "restricted zone" in entry.description


# ------------------------------------------------------------------ parameter resolution


def test_parameters_resolve_outermost_first() -> None:
    desc = descriptor(params={"a": 1, "b": 2, "c": 3})
    merged = descriptor_params(desc, {"b": 20}, {"c": 300})
    assert merged == {"a": 1, "b": 20, "c": 300}


def test_resolution_does_not_mutate_the_descriptor() -> None:
    desc = descriptor(params={"a": 1})
    descriptor_params(desc, {"a": 99})
    assert dict(desc.default_params) == {"a": 1}


# ------------------------------------------------------------------ third-party entry points


def test_entry_point_discovery_loads_a_module_package(monkeypatch: pytest.MonkeyPatch) -> None:
    package = fake_package("thirdparty", MODULE=descriptor("thirdparty"), create=lambda: None)
    entry = _StubEntryPoint("thirdparty", package)
    monkeypatch.setattr("vigil.events.registry.entry_points", lambda group: (entry,))
    found = discover_entry_points()
    assert [r.module_id for r in found] == ["thirdparty"]
    assert found[0].source == "entry_point:thirdparty"


def test_a_broken_entry_point_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin that fails to load silently is indistinguishable from one not installed."""

    class Exploding:
        name = "broken"

        def load(self) -> object:
            raise ImportError("no such dependency")

    monkeypatch.setattr("vigil.events.registry.entry_points", lambda group: (Exploding(),))
    with pytest.raises(ModuleError, match="failed to load"):
        discover_entry_points()


def test_an_entry_point_that_is_not_a_package_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _StubEntryPoint("notapackage", object())
    monkeypatch.setattr("vigil.events.registry.entry_points", lambda group: (entry,))
    with pytest.raises(ModuleError, match="must point at a module package"):
        discover_entry_points()


def test_entry_point_discovery_is_sorted(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = tuple(
        _StubEntryPoint(name, fake_package(name, MODULE=descriptor(name), create=lambda: None))
        for name in ("zulu", "alpha")
    )
    monkeypatch.setattr("vigil.events.registry.entry_points", lambda group: entries)
    assert [r.module_id for r in discover_entry_points()] == ["alpha", "zulu"]


def test_build_registry_can_combine_builtin_and_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    extra = registration(descriptor("injected"), "test:injected")
    registry = build_registry(include_entry_points=False, extra=[extra])
    assert registry.ids() == tuple(sorted((*SHIPPED, "injected")))


class _StubEntryPoint:
    def __init__(self, name: str, loaded: object) -> None:
        self.name = name
        self._loaded = loaded

    def load(self) -> object:
        return self._loaded
