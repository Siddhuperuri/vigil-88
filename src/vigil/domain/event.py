"""Accumulator state, candidate and verified events (05 §4). Engines arrive in P3."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vigil.core.ids import is_valid_ulid, validate_camera_id
from vigil.domain._validate import (
    require_finite,
    require_non_empty,
    require_non_negative,
    require_ratio,
    require_utc,
)
from vigil.domain.enums import EventType, ValidatorOutcome
from vigil.domain.observation import Observation, SubjectKey


@dataclass(frozen=True, slots=True)
class AccumulatorState:
    camera_id: str
    module_id: str
    subject: SubjectKey
    log_odds: float
    confidence_ratio: float
    observation_count: int
    support_ratio: float
    first_observation_ns: int
    last_observation_ns: int
    is_active: bool

    def __post_init__(self) -> None:
        validate_camera_id(self.camera_id)
        require_finite("log_odds", self.log_odds)
        require_ratio("confidence_ratio", self.confidence_ratio)
        require_ratio("support_ratio", self.support_ratio)
        require_non_negative("observation_count", self.observation_count)
        if self.last_observation_ns < self.first_observation_ns:
            raise ValueError("last_observation_ns precedes first_observation_ns")


@dataclass(frozen=True, slots=True)
class ValidatorVerdict:
    """Every verdict carries a reason, including passes (06 §6)."""

    validator_id: str
    outcome: ValidatorOutcome
    reason: str

    def __post_init__(self) -> None:
        require_non_empty("ValidatorVerdict.validator_id", self.validator_id)
        require_non_empty("ValidatorVerdict.reason", self.reason)


@dataclass(frozen=True, slots=True)
class CandidateEvent:
    candidate_id: str
    camera_id: str
    module_id: str
    event_type: EventType
    subject: SubjectKey
    accumulator: AccumulatorState
    observations: tuple[Observation, ...]
    involved_track_ids: frozenset[int]
    zone_ids: frozenset[str]
    promoted_monotonic_ns: int
    promoted_wall_utc: datetime

    def __post_init__(self) -> None:
        if not is_valid_ulid(self.candidate_id):
            raise ValueError("candidate_id must be a ULID")
        validate_camera_id(self.camera_id)
        require_utc("promoted_wall_utc", self.promoted_wall_utc)
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "involved_track_ids", frozenset(self.involved_track_ids))
        object.__setattr__(self, "zone_ids", frozenset(self.zone_ids))


@dataclass(frozen=True, slots=True)
class VerifiedEvent:
    candidate: CandidateEvent
    verdicts: tuple[ValidatorVerdict, ...]
    confidence_ratio: float
    verified_monotonic_ns: int

    def __post_init__(self) -> None:
        require_ratio("confidence_ratio", self.confidence_ratio)
        if any(v.outcome is ValidatorOutcome.REJECT for v in self.verdicts):
            raise ValueError("a VerifiedEvent cannot carry a REJECT verdict")
        object.__setattr__(self, "verdicts", tuple(self.verdicts))
