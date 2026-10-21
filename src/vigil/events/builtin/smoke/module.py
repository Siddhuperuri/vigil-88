"""The `smoke` module: a **declaration**, not a detector (ADR 0015).

The same conclusion as `fire` and for the same reason — no usable model — but smoke deserves its
own entry rather than being folded into fire, because 06 §7's **merge** rule is defined over two
distinct event types and because the two differ in what would be needed to do them properly.

**Smoke is the harder of the two.** Fire has a colour and a flicker signature, however
confusable. Smoke is defined by what it does to the scene behind it: contrast falls, edges
soften, a region drifts and grows without a boundary. A single frame of thin smoke is close to
indistinguishable from a soft-focus region, haze, a dirty lens, steam from a vent, or breath on a
cold morning. Doing it honestly needs temporal texture over tens of seconds, which means it needs
labelled video, not labelled stills — and the public fire/smoke datasets are overwhelmingly
stills. That is a sharper gap than a missing model file.

So this module declares `Capability.CLASSIFICATION`, nothing in this build grants it
(`core.capabilities.UNGRANTABLE`), the registry reports it UNAVAILABLE on every camera with an
actionable reason, and it never runs. `observe()` raises rather than returning an empty sequence:
"no observations" would read as "no smoke", which this module cannot determine.
"""

from __future__ import annotations

from collections.abc import Sequence

from vigil.core.capabilities import HOW_TO_PROVIDE, Capability
from vigil.core.errors import CapabilityError
from vigil.core.protocols.module import ModuleContext, ModuleDescriptor
from vigil.domain.enums import EventType
from vigil.domain.observation import Observation
from vigil.domain.scene import SceneState

MODULE_ID = "smoke"

MODULE = ModuleDescriptor(
    module_id=MODULE_ID,
    event_type=EventType.SMOKE,
    # See `fire`: there is no behaviour to version, and a 1.x would make the dismissal rate
    # attribute operator judgements to an algorithm that never ran.
    version="0.0.0",
    requires=frozenset({Capability.CLASSIFICATION}),
    optional=frozenset(),
    default_params={},
    description=(
        "NOT IMPLEMENTED. Smoke detection needs a classifier, and no smoke model with a "
        "compatible licence, checkable training-data provenance and a published held-out "
        "evaluation could be sourced (ADR 0015). Smoke also needs labelled *video* rather than "
        "stills, because it is defined by how a region changes over tens of seconds. This "
        "module declares the requirement so the gap is reported rather than silent; it produces "
        "no observations and never runs."
    ),
)


class SmokeModule:
    """Holds no state and makes no observation. Present so the gap is declared, not hidden."""

    __slots__ = ("_camera_id",)

    def __init__(self) -> None:
        self._camera_id = ""

    @property
    def descriptor(self) -> ModuleDescriptor:
        return MODULE

    def bind(self, ctx: ModuleContext) -> None:
        """Binding is itself a warning: the capability gate should have excluded this module."""
        self._camera_id = ctx.camera_id
        ctx.logger.warning(
            "declaration_only_module_bound",
            module_id=MODULE_ID,
            camera_id=ctx.camera_id,
            reason=(
                "smoke has no detector; it should be UNAVAILABLE on every camera because "
                "nothing grants classification"
            ),
        )

    def observe(self, scene: SceneState) -> Sequence[Observation]:
        raise CapabilityError(
            f"{MODULE_ID} has no detector and cannot observe: returning no observations would "
            f"be indistinguishable from reporting no smoke. Needs "
            f"{Capability.CLASSIFICATION.value}: {HOW_TO_PROVIDE[Capability.CLASSIFICATION]}",
            context={"module_id": MODULE_ID, "camera_id": self._camera_id},
        )

    def release(self) -> None:
        self._camera_id = ""


def create() -> SmokeModule:
    return SmokeModule()
