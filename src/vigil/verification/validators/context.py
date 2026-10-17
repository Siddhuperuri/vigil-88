"""What a validator is allowed to look at, beyond the candidate itself (04 §3.6).

`VerificationContext` in `core/protocols/` carries the camera's health and the scene. The
validators here need three more things that are pipeline state rather than frame state — the
suppression ledger, the operator's mutes, and the module's availability — so this module defines
the richer context and the verdict helpers the chain shares.

Keeping this separate from the validators means a validator is a small pure function of
(candidate, context), which is what makes each one testable in four lines.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from vigil.config.schema.modules import VerificationConfig
from vigil.core.capabilities import Availability
from vigil.domain.camera import CameraHealth
from vigil.domain.enums import EventType, ValidatorOutcome
from vigil.domain.event import ValidatorVerdict
from vigil.domain.observation import SubjectKey
from vigil.domain.scene import SceneState
from vigil.verification.ledger import SuppressionLedger


@dataclass(frozen=True, slots=True)
class Mute:
    """An operator saying "I know, the contractors are working there until 5" (06 §10).

    A mute is never silent: it is visible on the status bar with a countdown, because a muted
    camera that looks normal is how a real incident gets missed.
    """

    camera_id: str
    event_type: EventType | None = None
    zone_id: str | None = None
    expires_monotonic_ns: int | None = None
    reason: str = "operator mute"

    def covers(
        self,
        *,
        camera_id: str,
        event_type: EventType,
        zone_ids: frozenset[str],
        now_ns: int,
    ) -> bool:
        if self.camera_id != camera_id:
            return False
        if self.expires_monotonic_ns is not None and now_ns >= self.expires_monotonic_ns:
            return False
        if self.event_type is not None and self.event_type is not event_type:
            return False
        return not (self.zone_id is not None and self.zone_id not in zone_ids)


@dataclass(frozen=True, slots=True)
class VerificationState:
    """The full context the chain runs against."""

    config: VerificationConfig
    ledger: SuppressionLedger
    now_monotonic_ns: int
    camera_health: CameraHealth | None = None
    scene: SceneState | None = None
    module_availability: Availability = Availability.ACTIVE
    missing_optional: tuple[str, ...] = ()
    mutes: tuple[Mute, ...] = ()
    instability_since_monotonic_ns: Mapping[str, int] = field(default_factory=dict)
    first_deferred_monotonic_ns: int | None = None


def verdict(validator_id: str, outcome: ValidatorOutcome, reason: str) -> ValidatorVerdict:
    return ValidatorVerdict(validator_id=validator_id, outcome=outcome, reason=reason)


def passed(validator_id: str, reason: str) -> ValidatorVerdict:
    """PASS means "I have no objection", never "I am confident" (06 §6)."""
    return verdict(validator_id, ValidatorOutcome.PASS, reason)


def rejected(validator_id: str, reason: str) -> ValidatorVerdict:
    return verdict(validator_id, ValidatorOutcome.REJECT, reason)


def deferred(validator_id: str, reason: str) -> ValidatorVerdict:
    """DEFER means "not yet decidable" — it lets a validator wait for evidence without
    modelling time itself."""
    return verdict(validator_id, ValidatorOutcome.DEFER, reason)


def subject_track_id(subject: SubjectKey) -> int | None:
    if subject.kind != "track":
        return None
    try:
        return int(subject.value)
    except ValueError:
        return None
