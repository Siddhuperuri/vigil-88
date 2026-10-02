"""Tracking, scene, temporal, verification and module settings (04 §6, 06).

These engines arrive in P2/P3. The schema exists now so thresholds have one validated home
and no magic number ever lives in source. Cross-field invariants are enforced here.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel


class TrackingConfig(ConfigModel):
    max_age_frames: int = Field(default=30, ge=1)
    min_hits: int = Field(default=3, ge=1)
    iou_match_threshold_ratio: float = Field(default=0.3, ge=0.0, le=1.0)
    history_length: int = Field(default=90, ge=2)
    min_history_for_velocity: int = Field(default=5, ge=2)

    @model_validator(mode="after")
    def _history(self) -> Self:
        if self.min_history_for_velocity > self.history_length:
            raise ValueError("min_history_for_velocity cannot exceed history_length")
        return self


class SceneConfig(ConfigModel):
    region_grid: int = Field(default=4, ge=1, le=32)


class TemporalConfig(ConfigModel):
    half_life_ms: float = Field(default=2000.0, gt=0)
    signal_floor: float = Field(default=0.02, gt=0.0, lt=0.5)
    l_bound: float = Field(default=8.0, gt=0)
    l_activate: float = Field(default=2.2, gt=0)
    l_release: float = Field(default=0.8, ge=0)
    confidence_scale: float = Field(default=1.0, gt=0)
    min_observations: int = Field(default=4, ge=1)
    min_support_ratio: float = Field(default=0.6, ge=0.0, le=1.0)
    min_duration_ms: int = Field(default=1200, ge=0)
    release_sustain_ms: int = Field(default=2000, ge=0)

    @model_validator(mode="after")
    def _hysteresis(self) -> Self:
        if self.l_release >= self.l_activate:
            raise ValueError(
                "l_release must be below l_activate: the gap is the hysteresis that stops "
                "an incident flapping (04 §6.3)"
            )
        if self.l_activate > self.l_bound:
            raise ValueError("l_activate cannot exceed l_bound, or nothing could ever confirm")
        return self


class VerificationConfig(ConfigModel):
    max_defer_ms: int = Field(default=10_000, ge=0)
    cooldown_ms: int = Field(default=60_000, ge=0)
    instability_blackout_ms: int = Field(default=3000, ge=0)
    min_track_hit_ratio: float = Field(default=0.5, ge=0.0, le=1.0)


class ModulesConfig(ConfigModel):
    """`enabled` is an explicit allowlist. Discovering a module never enables it."""

    observe_budget_ms: float = Field(default=5.0, gt=0)
    quarantine_threshold: int = Field(default=3, ge=1)
    quarantine_retry_ms: int = Field(default=30_000, ge=0)
    enabled: tuple[str, ...] = ()
