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
from vigil.domain.enums import CandidateOutcome, EventType, ValidatorOutcome
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
class AccumulatorSample:
    """One point on an accumulator's trajectory. The sequence of these is what makes a decision
    reconstructable months later (01 §7.5), so it is captured as the decision is made and never
    recomputed from current config."""

    t_monotonic_ns: int
    log_odds: float
    confidence_ratio: float

    def __post_init__(self) -> None:
        require_ratio("confidence_ratio", self.confidence_ratio)


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


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    """The durable account of one candidate, whatever became of it (05 §5).

    Written for rejections as well as confirmations. A rejected candidate must still answer:
    what was considered (`candidate.observations`), why it was considered (their `facts` and the
    accumulator's state), how confidence evolved (`accumulator_trace`), and why it was refused
    (`rejection_reason`, with every verdict kept). Without that, tuning suppression means
    guessing (06 §6).

    P3 produces these records; P4 persists them. Retained under `retention.candidate_days`.
    """

    candidate: CandidateEvent
    outcome: CandidateOutcome
    verdicts: tuple[ValidatorVerdict, ...]
    accumulator_trace: tuple[AccumulatorSample, ...]
    rejection_reason: str | None
    decided_monotonic_ns: int
    decided_wall_utc: datetime

    def __post_init__(self) -> None:
        require_utc("decided_wall_utc", self.decided_wall_utc)
        if self.outcome is CandidateOutcome.REJECTED and not (self.rejection_reason or "").strip():
            raise ValueError("a REJECTED candidate must carry a rejection_reason")
        if self.outcome is CandidateOutcome.VERIFIED and self.rejection_reason is not None:
            raise ValueError("a VERIFIED candidate cannot carry a rejection_reason")
        object.__setattr__(self, "verdicts", tuple(self.verdicts))
        object.__setattr__(self, "accumulator_trace", tuple(self.accumulator_trace))

    @property
    def camera_id(self) -> str:
        return self.candidate.camera_id

    @property
    def event_type(self) -> EventType:
        return self.candidate.event_type

    @property
    def subject(self) -> SubjectKey:
        return self.candidate.subject
