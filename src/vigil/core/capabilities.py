"""Capability model (D-008, 04 §4).

A capability is something the *running configuration* can actually supply. It is granted
by the component that supplies it and by nothing else. Nothing is declared available
because it is planned, configured or hoped for. A module whose required capabilities are
missing is UNAVAILABLE, with a reason an operator can act on.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Capability(StrEnum):
    DETECTION = "detection"
    CLASSIFICATION = "classification"
    SEGMENTATION = "segmentation"
    TRACKING = "tracking"
    VELOCITY = "velocity"
    POSE = "pose"
    ZONES = "zones"
    GROUND_PLANE = "ground_plane"
    OPTICAL_FLOW = "optical_flow"
    SCENE_STABILITY = "scene_stability"


class Availability(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


# What would have to exist for each capability to be granted. Shown to operators.
HOW_TO_PROVIDE: Mapping[Capability, str] = MappingProxyType(
    {
        Capability.DETECTION: "a detector backend that produces detections (not the null detector)",
        Capability.CLASSIFICATION: (
            "a classifier model and engine. P6 searched for one and found none it could use: "
            "every fire/smoke model with usable accuracy is AGPL-3.0, unlicensed, or has "
            "training data of unknown provenance (ADR 0015). Supplying one means sourcing "
            "weights with a compatible licence and evaluating them on a labelled held-out set"
        ),
        Capability.SEGMENTATION: "a segmentation model and engine (not planned)",
        Capability.TRACKING: "the tracking engine, enabled by tracking.enabled",
        Capability.VELOCITY: (
            "the tracking engine; a velocity is reported once a track has "
            "tracking.min_history_for_velocity matched points"
        ),
        Capability.POSE: "a pose model (not planned)",
        Capability.ZONES: (
            "a zone file for this camera: set cameras.<id>.zones_ref and create "
            "config/zones/<ref>.zones.yaml with at least one zone"
        ),
        Capability.GROUND_PLANE: (
            "a calibration file for this camera: set cameras.<id>.calibration_ref and "
            "create config/calibration/<ref>.calib.yaml with four measured points"
        ),
        Capability.OPTICAL_FLOW: "an optical-flow stage (not planned)",
        Capability.SCENE_STABILITY: (
            "the scene analyzer with an array backend installed (the capture extra), "
            "and scene.stability_enabled"
        ),
    }
)


# Capabilities **no component in this build can grant**, whatever the configuration says, with
# the reason. This is a stronger statement than "not currently granted": a `ZONES` gap closes by
# writing a zone file, while nothing an operator can do closes these, because the code that would
# supply them does not exist here.
#
# It is a declared fact rather than a comment so that three things can be mechanical: the module
# API and `vigil doctor` can say "this build cannot grant it" instead of implying a missing file;
# the contract suite can tell a module that *observes* from one that only *declares*; and a module
# shipped as a declaration cannot drift into looking operational.
UNGRANTABLE: Mapping[Capability, str] = MappingProxyType(
    {
        Capability.CLASSIFICATION: "no classifier engine exists in this build (ADR 0015)",
        Capability.SEGMENTATION: "no segmentation engine exists in this build; not planned",
        Capability.POSE: "no pose model or engine exists in this build; not planned",
        Capability.OPTICAL_FLOW: "no optical-flow stage exists in this build; not planned",
    }
)


def ungrantable(requires: Iterable[Capability]) -> frozenset[Capability]:
    """Which of `requires` nothing in this build can supply. Empty means it could run here."""
    return frozenset(requires) & frozenset(UNGRANTABLE)


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    capability: Capability
    available: bool
    provider: str | None
    how_to_provide: str | None


@dataclass(frozen=True, slots=True)
class AvailabilityReport:
    status: Availability
    missing_required: frozenset[Capability]
    missing_optional: frozenset[Capability]
    reason: str | None


def resolve_availability(
    requires: Iterable[Capability],
    optional: Iterable[Capability],
    available: frozenset[Capability],
) -> AvailabilityReport:
    missing_required = frozenset(requires) - available
    missing_optional = frozenset(optional) - available
    if missing_required:
        parts = ", ".join(
            f"{c.value} (needs {HOW_TO_PROVIDE[c]})" for c in sorted(missing_required)
        )
        return AvailabilityReport(
            Availability.UNAVAILABLE,
            missing_required,
            missing_optional,
            f"missing required capabilities: {parts}",
        )
    if missing_optional:
        names = ", ".join(sorted(c.value for c in missing_optional))
        return AvailabilityReport(
            Availability.DEGRADED,
            missing_required,
            missing_optional,
            f"running without optional capabilities: {names}",
        )
    return AvailabilityReport(Availability.ACTIVE, missing_required, missing_optional, None)


class CapabilityLedger:
    """Who has granted what. Thread-safe. Starts empty: nothing is available by default."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._granted: dict[Capability, str] = {}

    def grant(self, capability: Capability, provider: str) -> None:
        with self._lock:
            self._granted[capability] = provider

    def revoke(self, capability: Capability) -> None:
        with self._lock:
            self._granted.pop(capability, None)

    def available(self) -> frozenset[Capability]:
        with self._lock:
            return frozenset(self._granted)

    def status(self, capability: Capability) -> CapabilityStatus:
        with self._lock:
            provider = self._granted.get(capability)
        if provider is None:
            return CapabilityStatus(capability, False, None, HOW_TO_PROVIDE[capability])
        return CapabilityStatus(capability, True, provider, None)

    def table(self) -> tuple[CapabilityStatus, ...]:
        return tuple(self.status(c) for c in Capability)
