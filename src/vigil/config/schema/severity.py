"""Severity weights and bands (06 §8)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Self

from pydantic import Field, field_validator, model_validator

from vigil.config.schema.base import ConfigModel
from vigil.config.schema.taxonomy import EVENT_TYPE_NAMES
from vigil.config.schema.zones import parse_clock

WEIGHT_SUM_TOLERANCE = 1e-6


class SeverityWeights(ConfigModel):
    base: float = Field(default=0.30, ge=0, le=1)
    zone_criticality: float = Field(default=0.20, ge=0, le=1)
    confidence: float = Field(default=0.15, ge=0, le=1)
    object_count: float = Field(default=0.10, ge=0, le=1)
    persistence: float = Field(default=0.10, ge=0, le=1)
    time_of_day: float = Field(default=0.05, ge=0, le=1)
    escalation: float = Field(default=0.05, ge=0, le=1)
    camera_trust: float = Field(default=0.05, ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        total = sum(self.model_dump().values())
        if not math.isclose(total, 1.0, abs_tol=WEIGHT_SUM_TOLERANCE):
            raise ValueError(
                f"severity weights must sum to 1.0, got {total:.6f}: a larger sum silently "
                "saturates every incident at CRITICAL"
            )
        return self


class SeverityBands(ConfigModel):
    """Lower score bound of each band. Scores below `LOW` are INFO."""

    LOW: float = Field(default=20.0, ge=0, le=100)
    MODERATE: float = Field(default=40.0, ge=0, le=100)
    HIGH: float = Field(default=65.0, ge=0, le=100)
    CRITICAL: float = Field(default=85.0, ge=0, le=100)

    @model_validator(mode="after")
    def _ascending(self) -> Self:
        if not self.LOW < self.MODERATE < self.HIGH < self.CRITICAL:
            raise ValueError("severity bands must be strictly ascending LOW < ... < CRITICAL")
        return self


class TimeWindow(ConfigModel):
    """A window of the day with a severity weight. May wrap midnight (22:00-06:00)."""

    start: str
    end: str
    weight_ratio: float = Field(ge=0.0, le=1.0)

    @field_validator("start", "end")
    @classmethod
    def _clock(cls, value: str) -> str:
        parse_clock(value)
        return value

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if parse_clock(self.start) == parse_clock(self.end):
            raise ValueError("a time window must not be empty")
        return self

    def covers(self, minute_of_day: int) -> bool:
        start, end = parse_clock(self.start), parse_clock(self.end)
        if start < end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end


class SeverityConfig(ConfigModel):
    """Severity is derived from declared factors, never from a UI label (06 §8)."""

    weights: SeverityWeights = SeverityWeights()
    bands: SeverityBands = SeverityBands()

    # Intrinsic seriousness per event type, in [0, 1]. An unlisted type gets `base_default`.
    base_by_type: Mapping[str, float] = Field(
        default_factory=lambda: {
            "fire": 1.0,
            "smoke": 0.8,
            "person_down": 0.85,
            "vehicle_collision": 0.8,
            "intrusion": 0.65,
            "restricted_area_entry": 0.5,
            "dangerous_motion": 0.55,
            "crowd_anomaly": 0.5,
            "abandoned_object": 0.45,
            "vehicle_anomaly": 0.35,
        }
    )
    base_default: float = Field(default=0.5, ge=0.0, le=1.0)

    # Saturation points. Above these, more is not proportionally worse (06 §8).
    count_saturation: float = Field(default=5.0, gt=0.0)
    persistence_saturation_ms: float = Field(default=60_000.0, gt=0.0)
    escalation_saturation: float = Field(default=5.0, gt=0.0)

    # Out-of-hours policy. Evaluated in order; the first window that covers the time wins.
    time_policy: tuple[TimeWindow, ...] = Field(
        default_factory=lambda: (
            TimeWindow(start="22:00", end="06:00", weight_ratio=1.0),
            TimeWindow(start="06:00", end="08:00", weight_ratio=0.6),
            TimeWindow(start="18:00", end="22:00", weight_ratio=0.6),
        )
    )
    time_default_weight_ratio: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _known_types(self) -> Self:
        # The names come from `schema/taxonomy.py` rather than from `domain.enums`, because
        # config (L1) must not import domain (L2). A test proves the mirror is exact.
        unknown = sorted(set(self.base_by_type) - EVENT_TYPE_NAMES)
        if unknown:
            raise ValueError(
                f"severity.base_by_type names unknown event types: {', '.join(unknown)}"
            )
        bad = sorted(k for k, v in self.base_by_type.items() if not 0.0 <= v <= 1.0)
        if bad:
            raise ValueError(f"severity.base_by_type values must be in [0, 1]: {', '.join(bad)}")
        return self
