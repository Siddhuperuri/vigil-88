"""Retention policy (01 §10, 05 §8). Defaults are finite, never infinite."""

from __future__ import annotations

from pydantic import Field

from vigil.config.schema.base import ConfigModel


class RetentionConfig(ConfigModel):
    incident_days: int = Field(default=365, ge=1)
    clip_days: int = Field(default=30, ge=1)
    snapshot_days: int = Field(default=90, ge=1)
    candidate_days: int = Field(default=7, ge=1)
    metric_days: int = Field(default=14, ge=1)
    sweep_interval_s: int = Field(default=3600, ge=60)
