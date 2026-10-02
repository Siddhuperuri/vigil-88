from __future__ import annotations

import pytest

from vigil.core.capabilities import (
    HOW_TO_PROVIDE,
    Availability,
    Capability,
    CapabilityLedger,
    resolve_availability,
)
from vigil.core.errors import CapabilityError
from vigil.core.registry import PluginRegistry

C = Capability


def test_nothing_is_available_by_default() -> None:
    ledger = CapabilityLedger()
    assert ledger.available() == frozenset()
    assert not any(s.available for s in ledger.table())


def test_every_capability_explains_how_to_obtain_it() -> None:
    assert set(HOW_TO_PROVIDE) == set(Capability)
    assert all(HOW_TO_PROVIDE[c].strip() for c in Capability)


def test_grant_records_the_provider_and_revoke_removes_it() -> None:
    ledger = CapabilityLedger()
    ledger.grant(C.DETECTION, "yolo")
    s = ledger.status(C.DETECTION)
    assert s.available and s.provider == "yolo" and s.how_to_provide is None
    ledger.revoke(C.DETECTION)
    ledger.revoke(C.DETECTION)  # idempotent
    s = ledger.status(C.DETECTION)
    assert not s.available and s.provider is None and s.how_to_provide


def test_table_covers_every_capability() -> None:
    assert {s.capability for s in CapabilityLedger().table()} == set(Capability)


def test_active_when_everything_required_is_present() -> None:
    r = resolve_availability({C.DETECTION}, set(), frozenset({C.DETECTION}))
    assert r.status is Availability.ACTIVE and r.reason is None


def test_degraded_when_only_optional_is_missing() -> None:
    r = resolve_availability({C.DETECTION}, {C.VELOCITY}, frozenset({C.DETECTION}))
    assert r.status is Availability.DEGRADED
    assert r.missing_optional == {C.VELOCITY} and "velocity" in (r.reason or "")


def test_unavailable_names_the_missing_capability_and_the_fix() -> None:
    r = resolve_availability({C.DETECTION, C.GROUND_PLANE}, set(), frozenset({C.DETECTION}))
    assert r.status is Availability.UNAVAILABLE
    assert r.missing_required == {C.GROUND_PLANE}
    assert "ground_plane" in (r.reason or "") and "homography" in (r.reason or "")


def test_missing_required_outranks_missing_optional() -> None:
    r = resolve_availability({C.TRACKING}, {C.VELOCITY}, frozenset())
    assert r.status is Availability.UNAVAILABLE


def test_registry_gates_plugins_by_capability() -> None:
    reg: PluginRegistry[str] = PluginRegistry()
    reg.register("intrusion", "I", requires={C.DETECTION, C.TRACKING, C.ZONES})
    reg.register("fire", "F", requires={C.DETECTION}, optional={C.CLASSIFICATION})
    assert reg.ids() == ("fire", "intrusion")

    none = frozenset[Capability]()
    assert reg.runnable(none) == ()
    only_detection = frozenset({C.DETECTION})
    assert reg.runnable(only_detection) == ("fire",)
    assert reg.availability("fire", only_detection).status is Availability.DEGRADED
    assert reg.availability("intrusion", only_detection).status is Availability.UNAVAILABLE
    assert set(reg.runnable(frozenset({C.DETECTION, C.TRACKING, C.ZONES}))) == {"fire", "intrusion"}


def test_registry_rejects_duplicates_and_unknown_ids() -> None:
    reg: PluginRegistry[str] = PluginRegistry()
    reg.register("a", "A", requires=())
    with pytest.raises(CapabilityError, match="already registered"):
        reg.register("a", "A2", requires=())
    with pytest.raises(CapabilityError, match="unknown plugin"):
        reg.get("zzz")
    with pytest.raises(CapabilityError, match="unknown plugin"):
        reg.availability("zzz", frozenset())
    assert reg.get("a") == "A"
