"""Incident, its audit trail and its explanation (05 §4, 06 §9).

An incident is an immutable historical fact: model and system metadata are copied onto it
rather than referenced, so it never changes meaning when configuration changes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from vigil.core.ids import is_valid_ulid, validate_camera_id
from vigil.domain._validate import (
    frozen_mapping,
    require_non_empty,
    require_non_negative,
    require_ratio,
    require_utc,
)
from vigil.domain.detection import ModelDescriptor
from vigil.domain.enums import EventType, IncidentStatus, Severity
from vigil.domain.event import ValidatorVerdict
from vigil.domain.observation import FactValue

DISPLAY_REF_RE = re.compile(r"^INC-\d{8}-[0-9A-Z]{6}$")
MAX_SEVERITY_SCORE = 100.0

Actor = Literal["system", "operator"]


@dataclass(frozen=True, slots=True)
class ObservationSummary:
    t_monotonic_ns: int
    signal: float
    facts: Mapping[str, FactValue]

    def __post_init__(self) -> None:
        require_ratio("signal", self.signal)
        object.__setattr__(self, "facts", frozen_mapping(self.facts))


@dataclass(frozen=True, slots=True)
class AccumulatorSample:
    t_monotonic_ns: int
    log_odds: float
    confidence_ratio: float

    def __post_init__(self) -> None:
        require_ratio("confidence_ratio", self.confidence_ratio)


@dataclass(frozen=True, slots=True)
class SeverityFactor:
    name: str
    raw_value: float
    weight: float
    contribution: float

    def __post_init__(self) -> None:
        require_non_empty("SeverityFactor.name", self.name)
        require_ratio("raw_value", self.raw_value)
        require_ratio("weight", self.weight)
        require_non_negative("contribution", self.contribution)


@dataclass(frozen=True, slots=True)
class SeverityAssessment:
    score: float
    severity: Severity
    factors: tuple[SeverityFactor, ...]
    degraded: bool

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= MAX_SEVERITY_SCORE:
            raise ValueError("severity score must be in [0, 100]")
        object.__setattr__(self, "factors", tuple(self.factors))


@dataclass(frozen=True, slots=True)
class IncidentTransition:
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    at_wall_utc: datetime
    actor: Actor
    reason: str

    def __post_init__(self) -> None:
        require_utc("at_wall_utc", self.at_wall_utc)
        require_non_empty("IncidentTransition.reason", self.reason)


@dataclass(frozen=True, slots=True)
class IncidentExplanation:
    trigger_summary: str
    observations: tuple[ObservationSummary, ...]
    accumulator_trace: tuple[AccumulatorSample, ...]
    validator_verdicts: tuple[ValidatorVerdict, ...]
    severity_factors: tuple[SeverityFactor, ...]
    thresholds: Mapping[str, float]
    capability_caveats: tuple[str, ...]

    def __post_init__(self) -> None:
        require_non_empty("trigger_summary", self.trigger_summary)
        for name in (
            "observations",
            "accumulator_trace",
            "validator_verdicts",
            "severity_factors",
            "capability_caveats",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "thresholds", frozen_mapping(self.thresholds))


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    version: str
    config_hash: str
    device: str
    pipeline_fps: float | None
    inference_latency_p95_ms: float | None

    def __post_init__(self) -> None:
        require_non_empty("version", self.version)
        require_non_empty("config_hash", self.config_hash)


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: str
    display_ref: str
    camera_id: str
    event_type: EventType
    module_id: str
    module_version: str
    status: IncidentStatus
    severity: Severity
    severity_score: float
    confidence_ratio: float
    zone_ids: frozenset[str]
    location: str | None
    first_observed_wall_utc: datetime
    confirmed_wall_utc: datetime
    last_observed_wall_utc: datetime
    resolved_wall_utc: datetime | None
    duration_ms: float | None
    involved_track_ids: frozenset[int]
    tracker_epoch: int
    evidence_ids: tuple[str, ...]
    explanation: IncidentExplanation
    transitions: tuple[IncidentTransition, ...]
    model_metadata: ModelDescriptor
    system_metadata: SystemSnapshot
    operator_note: str | None

    def __post_init__(self) -> None:
        if not is_valid_ulid(self.incident_id):
            raise ValueError("incident_id must be a ULID")
        if not DISPLAY_REF_RE.match(self.display_ref):
            raise ValueError(f"invalid display_ref {self.display_ref!r}")
        validate_camera_id(self.camera_id)
        if not 0.0 <= self.severity_score <= MAX_SEVERITY_SCORE:
            raise ValueError("severity_score must be in [0, 100]")
        require_ratio("confidence_ratio", self.confidence_ratio)
        for name in (
            "first_observed_wall_utc",
            "confirmed_wall_utc",
            "last_observed_wall_utc",
        ):
            require_utc(name, getattr(self, name))
        if self.resolved_wall_utc is not None:
            require_utc("resolved_wall_utc", self.resolved_wall_utc)
            if self.resolved_wall_utc < self.confirmed_wall_utc:
                raise ValueError("resolved precedes confirmed")
        if self.confirmed_wall_utc < self.first_observed_wall_utc:
            raise ValueError("confirmed precedes first_observed")
        if self.last_observed_wall_utc < self.first_observed_wall_utc:
            raise ValueError("last_observed precedes first_observed")
        if self.duration_ms is not None:
            require_non_negative("duration_ms", self.duration_ms)
        object.__setattr__(self, "zone_ids", frozenset(self.zone_ids))
        object.__setattr__(self, "involved_track_ids", frozenset(self.involved_track_ids))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "transitions", tuple(self.transitions))

    @property
    def detection_latency_ms(self) -> float:
        """confirmed - first_observed: the headline quality metric of the temporal engine."""
        return (self.confirmed_wall_utc - self.first_observed_wall_utc).total_seconds() * 1000.0
