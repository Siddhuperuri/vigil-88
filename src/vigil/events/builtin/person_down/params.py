"""person_down parameters.

Every threshold that decides anything lives here, validated, so no magic number sits in the
module. The three weights must sum to 1.0: a weight set that summed to 1.3 would saturate the
signal at its ceiling for any posture at all, which is the silent-failure mode this check
exists to prevent (the severity engine's weights are validated the same way, 06 §8).
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel
from vigil.domain.enums import ObjectClass

WEIGHT_SUM_TOLERANCE = 1e-6


class PersonDownParams(ConfigModel):
    object_classes: tuple[ObjectClass, ...] = (ObjectClass.PERSON,)

    # Track quality. A mostly-predicted track is hallucinated motion, and a box that is mostly
    # the filter's guess is the last thing to judge a posture from.
    min_track_hits: int = Field(default=4, ge=1)
    min_hit_ratio: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- term 1: aspect ratio. width / height of the box. A standing adult is roughly 0.3-0.5;
    # a person lying down is at or above 1.0. Between the two the term ramps linearly.
    upright_aspect_ratio: float = Field(default=0.55, gt=0.0)
    prone_aspect_ratio: float = Field(default=1.0, gt=0.0)

    # --- term 2: verticality collapse. Current height against the tallest the same track has
    # been in its own recent history. Someone who falls keeps their width and loses their
    # height, which is the strongest cue available without a pose model.
    collapse_ratio: float = Field(default=0.55, gt=0.0, le=1.0)
    no_collapse_ratio: float = Field(default=0.9, gt=0.0, le=1.0)
    # How many of the track's own recent measured points the standing reference is taken from.
    reference_window_frames: int = Field(default=60, ge=2)
    # A reference built from fewer points than this is not trustworthy, so the collapse term is
    # reported as unavailable rather than computed from one or two boxes.
    min_reference_points: int = Field(default=5, ge=2)

    # --- term 3: immobility, in box heights per second. Needs VELOCITY; without it the module
    # runs DEGRADED and this term is dropped, with the other weights renormalised.
    immobile_speed_ratio: float = Field(default=0.15, ge=0.0)
    moving_speed_ratio: float = Field(default=0.6, gt=0.0)

    aspect_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    collapse_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    immobility_weight: float = Field(default=0.2, ge=0.0, le=1.0)

    # The signal band. The floor is below neutral on purpose: a person who is plainly standing
    # and walking is evidence *against* this hypothesis, and saying so is what lets an event
    # release quickly when someone gets up (04 §6.1).
    signal_floor: float = Field(default=0.1, gt=0.0, lt=0.5)
    signal_ceiling: float = Field(default=0.9, gt=0.5, lt=1.0)

    # Added once the subject has been immobile for this long, which outlives the bounded
    # trajectory and is therefore the only term that can speak to minutes rather than seconds.
    stillness_bonus_after_ms: float = Field(default=5000.0, ge=0.0)
    stillness_bonus_ratio: float = Field(default=0.05, ge=0.0, le=0.3)

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if not self.object_classes:
            raise ValueError("object_classes must not be empty, or the module can never fire")
        total = self.aspect_weight + self.collapse_weight + self.immobility_weight
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"aspect_weight, collapse_weight and immobility_weight must sum to 1.0, not "
                f"{total:.3f}: a larger sum saturates the signal for any posture at all"
            )
        if self.upright_aspect_ratio >= self.prone_aspect_ratio:
            raise ValueError("upright_aspect_ratio must be below prone_aspect_ratio")
        if self.collapse_ratio >= self.no_collapse_ratio:
            raise ValueError("collapse_ratio must be below no_collapse_ratio")
        if self.immobile_speed_ratio >= self.moving_speed_ratio:
            raise ValueError("immobile_speed_ratio must be below moving_speed_ratio")
        if self.min_reference_points > self.reference_window_frames:
            raise ValueError("min_reference_points cannot exceed reference_window_frames")
        return self
