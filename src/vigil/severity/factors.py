"""The eight severity factors (06 §8).

Each is a pure function returning [0, 1], independently unit-testable, and recorded with its raw
value, weight and contribution. That is what makes severity explainable as arithmetic: an
operator who disagrees with a score can see which factor to argue with and which config key to
change.

Two properties the whole file exists to hold:

  * **Severity never reads confidence except as one declared factor.** It answers "how much does
    this matter", not "how sure are we", and the two must not be coupled — a dangerous event
    must not look more certain merely because it is dangerous.
  * **Saturating, not linear.** Fifty people is not ten times worse than five, and a fire burning
    two minutes is not forty times worse than one burning three seconds. Every unbounded input
    is divided by a configured saturation point and clamped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from vigil.config.schema.severity import SeverityConfig
from vigil.core.units import ns_to_ms
from vigil.domain.enums import EventType
from vigil.domain.event import VerifiedEvent
from vigil.domain.scene import Zone

MAX_CRITICALITY = 4.0


@dataclass(frozen=True, slots=True)
class FactorInputs:
    """Everything the factors need, gathered once so each factor stays a pure function."""

    event: VerifiedEvent
    now_wall_utc: datetime
    zones: Sequence[Zone] = ()
    camera_trust_ratio: float = 1.0
    related_event_count: int = 0

    @property
    def event_type(self) -> EventType:
        return self.event.candidate.event_type


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def factor_base(inputs: FactorInputs, config: SeverityConfig) -> float:
    """The event type's intrinsic seriousness. Fire is not an equal of an abandoned bag."""
    return _clamp(config.base_by_type.get(inputs.event_type.value, config.base_default))


def factor_zone_criticality(inputs: FactorInputs, config: SeverityConfig) -> float:
    """The most critical involved zone, normalised. A zone nobody rated contributes nothing."""
    involved = inputs.event.candidate.zone_ids
    criticalities = [z.criticality for z in inputs.zones if z.zone_id in involved]
    if not criticalities:
        return 0.0
    return _clamp(max(criticalities) / MAX_CRITICALITY)


def factor_confidence(inputs: FactorInputs, config: SeverityConfig) -> float:
    """Post-verification confidence. An uncertain event is still worth showing, lower."""
    return _clamp(inputs.event.confidence_ratio)


def factor_object_count(inputs: FactorInputs, config: SeverityConfig) -> float:
    """Saturating in the number of involved objects."""
    involved = max(len(inputs.event.candidate.involved_track_ids), 1)
    return _clamp(involved / config.count_saturation)


def factor_persistence(inputs: FactorInputs, config: SeverityConfig) -> float:
    """How long the evidence has been accumulating, saturating.

    Measured from the first contributing observation to promotion — the same span that makes
    `confirmed - first_observed` the detection latency (05 §4).
    """
    accumulator = inputs.event.candidate.accumulator
    duration_ms = ns_to_ms(
        max(accumulator.last_observation_ns - accumulator.first_observation_ns, 0)
    )
    return _clamp(duration_ms / config.persistence_saturation_ms)


def factor_time_of_day(inputs: FactorInputs, config: SeverityConfig) -> float:
    """An intrusion at 03:00 outranks one at 14:00.

    The policy is a list of windows with weights, so "out of hours" is whatever the site says it
    is rather than an assumption baked into code.
    """
    minute = inputs.now_wall_utc.hour * 60 + inputs.now_wall_utc.minute
    for window in config.time_policy:
        if window.covers(minute):
            return _clamp(window.weight_ratio)
    return _clamp(config.time_default_weight_ratio)


def factor_escalation(inputs: FactorInputs, config: SeverityConfig) -> float:
    """Related events in the recent window (06 §7), saturating."""
    return _clamp(inputs.related_event_count / config.escalation_saturation)


def factor_camera_trust(inputs: FactorInputs, config: SeverityConfig) -> float:
    """Per-camera multiplier reflecting that camera's history of false positives.

    Set by the operator, default 1.0. A camera that cries wolf contributes less.
    """
    return _clamp(inputs.camera_trust_ratio)


FACTORS: Mapping[str, object] = {
    "base": factor_base,
    "zone_criticality": factor_zone_criticality,
    "confidence": factor_confidence,
    "object_count": factor_object_count,
    "persistence": factor_persistence,
    "time_of_day": factor_time_of_day,
    "escalation": factor_escalation,
    "camera_trust": factor_camera_trust,
}
