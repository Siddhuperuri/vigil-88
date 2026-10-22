"""abandoned_object parameters.

Spatial thresholds are expressed as multiples of the **object's own box height**, never in
pixels, because a pixel distance silently means different things at 640x480 and 1920x1080, and
different things again for a bag two metres from the camera and one thirty metres away.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel
from vigil.domain.enums import ObjectClass, ZoneKind

WEIGHT_SUM_TOLERANCE = 1e-6


class AbandonedObjectParams(ConfigModel):
    # What can be abandoned. Normalized classes, never raw detector labels (04 §7).
    object_classes: tuple[ObjectClass, ...] = (ObjectClass.OBJECT_BAG, ObjectClass.OBJECT_BOX)
    # Which classes count as a possible owner. A bag next to a parked car is not unattended in
    # the sense that matters, but that needs a different module; here only people own things.
    owner_classes: tuple[ObjectClass, ...] = (ObjectClass.PERSON,)
    # Zone kinds to watch when the ZONES capability is granted. EXCLUSION is never included:
    # it marks area to ignore, and treating one as watched would invert its meaning.
    zone_kinds: tuple[ZoneKind, ...] = (
        ZoneKind.RESTRICTED,
        ZoneKind.REGION_OF_INTEREST,
        ZoneKind.COUNTING,
    )

    min_track_hits: int = Field(default=6, ge=1)
    min_hit_ratio: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- ownership. A person whose box comes within this many object-box heights of the object
    # is treated as plausibly attending it. Generous on purpose: wrongly deciding something is
    # unattended is this module's main false positive.
    owner_distance_ratio: float = Field(default=2.5, gt=0.0)
    # How long after the last plausible owner was seen nearby before separation counts as
    # evidence. This is the "owner left" half of the sequence that makes an object *abandoned*
    # rather than merely stationary.
    separation_after_ms: float = Field(default=10_000.0, ge=0.0)

    # --- persistence, from the tracker's own stationary judgement.
    persistence_after_ms: float = Field(default=30_000.0, ge=0.0)

    # --- the signal. `base_signal` is for a stationary, unattended object before either the
    # separation or persistence term applies.
    base_signal: float = Field(default=0.6, gt=0.5, lt=1.0)
    separation_bonus_ratio: float = Field(default=0.15, ge=0.0, le=0.4)
    persistence_bonus_ratio: float = Field(default=0.15, ge=0.0, le=0.4)
    # Emitted while a plausible owner is standing next to it: evidence *against*, which is what
    # releases an event when someone comes back for their bag (04 §6.1).
    attended_signal: float = Field(default=0.15, gt=0.0, lt=0.5)
    max_signal: float = Field(default=0.95, gt=0.5, lt=1.0)

    # Bound on how many objects one camera will reason about per frame, so a detector having a
    # bad day cannot make this module quadratic.
    max_objects: int = Field(default=32, ge=1)

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if not self.object_classes:
            raise ValueError("object_classes must not be empty, or the module can never fire")
        if not self.owner_classes:
            raise ValueError(
                "owner_classes must not be empty: with no owner class every object is "
                "permanently unattended and the module would fire on the furniture"
            )
        if ZoneKind.EXCLUSION in self.zone_kinds:
            raise ValueError(
                "an EXCLUSION zone marks area to ignore; watching one would invert the meaning "
                "of every exclusion an operator has drawn"
            )
        if set(self.object_classes) & set(self.owner_classes):
            raise ValueError(
                "a class cannot be both an object and its own owner: it would attend itself "
                "and the module could never fire"
            )
        ceiling = self.base_signal + self.separation_bonus_ratio + self.persistence_bonus_ratio
        if ceiling >= 1.0 + WEIGHT_SUM_TOLERANCE and self.max_signal >= 1.0:
            raise ValueError("base_signal plus the bonuses must stay below 1.0")
        return self
