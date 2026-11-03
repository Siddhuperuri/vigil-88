"""Line-crossing module parameters. An unknown key is an error at startup (06 §5)."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel
from vigil.domain.enums import ObjectClass


class LineCrossingParams(ConfigModel):
    object_classes: tuple[ObjectClass, ...] = (ObjectClass.PERSON,)
    # A crossing is a moment, and one frame of evidence cannot accumulate. So the module keeps
    # reporting a crossing for this long afterwards, read from the track's own trajectory. That
    # makes the module a pure function of the scene: it keeps no timer and no "already fired".
    lookback_ms: float = Field(default=4000.0, ge=500.0, le=60_000.0)
    min_track_hits: int = Field(default=3, ge=1)
    min_hit_ratio: float = Field(default=0.4, ge=0.0, le=1.0)
    # The signal for a plain, clear crossing; not a probability that an incident is occurring.
    base_signal: float = Field(default=0.72, gt=0.5, lt=1.0)
    # A track that crossed in the counted direction and is still moving that way is stronger.
    velocity_bonus_ratio: float = Field(default=0.10, ge=0.0, le=0.5)
    # Interpolated points are the tracker's guess, not an observation. A crossing seen only
    # between guesses is not reported.
    allow_interpolated: bool = False

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if not self.object_classes:
            raise ValueError("object_classes must not be empty, or the module can never fire")
        if self.base_signal + self.velocity_bonus_ratio >= 1.0:
            raise ValueError("base_signal plus the bonus must stay below 1.0")
        return self
