"""Intrusion module parameters.

`extra="forbid"` is inherited from `ConfigModel`, so an unknown key is an error at startup
(06 §5). That is deliberate: a typo in `base_signal` would otherwise leave the module running
with a default nobody chose.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel
from vigil.domain.enums import ObjectClass, ZoneKind


class IntrusionParams(ConfigModel):
    # What counts as an intruder. Normalized classes, never raw detector labels, so swapping
    # detectors touches one config file and not this module (04 §7).
    object_classes: tuple[ObjectClass, ...] = (ObjectClass.PERSON,)
    # Which zone kinds this module polices. EXCLUSION zones are never included: they are
    # suppression, and a module that treated one as restricted would invert its meaning.
    zone_kinds: tuple[ZoneKind, ...] = (ZoneKind.RESTRICTED,)

    # A track with fewer matched detections than this is not yet trustworthy enough to reason
    # about: it may be a single stray detection that will vanish next frame.
    min_track_hits: int = Field(default=3, ge=1)

    # The signal for a plain, unambiguous presence in a restricted zone. Not 1.0, and not a
    # probability that an incident is occurring: it is this frame's support for the hypothesis,
    # and accumulation decides the rest (04 §6.1).
    base_signal: float = Field(default=0.70, gt=0.5, lt=1.0)

    # Added when the subject is moving into the zone rather than along or out of it. Requires
    # the VELOCITY capability; without it the module runs DEGRADED and this is simply not
    # applied, which is why it is a bonus rather than part of the base.
    velocity_bonus_ratio: float = Field(default=0.15, ge=0.0, le=0.5)

    # Subtracted while the subject is within this distance of the zone boundary, as a fraction
    # of the frame's smaller dimension. Boundary flicker is this module's main false positive:
    # a person walking the line should accumulate more slowly than one standing well inside.
    edge_margin_ratio: float = Field(default=0.02, ge=0.0, le=0.25)
    edge_penalty_ratio: float = Field(default=0.15, ge=0.0, le=0.5)

    # Added once the subject has dwelt in the zone this long. Persistence is itself evidence:
    # someone still there after several seconds is less likely to have strayed in by accident.
    dwell_bonus_after_ms: float = Field(default=3000.0, ge=0.0)
    dwell_bonus_ratio: float = Field(default=0.10, ge=0.0, le=0.5)

    # A track that is mostly prediction is hallucinated motion. Below this hit ratio the module
    # emits nothing at all rather than a weak signal, because the input is not evidence.
    min_hit_ratio: float = Field(default=0.4, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if not self.object_classes:
            raise ValueError("object_classes must not be empty, or the module can never fire")
        if not self.zone_kinds:
            raise ValueError("zone_kinds must not be empty, or the module can never fire")
        if ZoneKind.EXCLUSION in self.zone_kinds:
            raise ValueError(
                "an EXCLUSION zone marks area to ignore; treating it as restricted would "
                "invert the meaning of every exclusion an operator has drawn"
            )
        if self.base_signal + self.velocity_bonus_ratio + self.dwell_bonus_ratio >= 1.0:
            raise ValueError(
                "base_signal plus the bonuses must stay below 1.0: a signal that can reach "
                "certainty from one frame defeats temporal accumulation"
            )
        return self
