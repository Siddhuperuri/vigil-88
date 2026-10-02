"""Severity weights and bands (06 §8)."""

from __future__ import annotations

import math
from typing import Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel

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


class SeverityConfig(ConfigModel):
    weights: SeverityWeights = SeverityWeights()
    bands: SeverityBands = SeverityBands()
