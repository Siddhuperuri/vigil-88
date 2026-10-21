"""The `fire` module: a **declaration**, not a detector (ADR 0015).

This module exists so that the absence of fire detection is a fact the system reports rather
than a gap a reader has to infer from a missing directory. It declares that it needs
`Capability.CLASSIFICATION`, nothing in this build can grant that (`core.capabilities.
UNGRANTABLE`), and so the registry reports it UNAVAILABLE on every camera with a reason naming
what would have to exist. It never runs.

**Why there is no detector here.** P6 looked for a fire/smoke model that was licensed
compatibly, had checkable provenance, and could be evaluated. Every candidate failed at least
one test: the accurate ones are Ultralytics derivatives under AGPL-3.0 (refused in ADR 0012 for
this system, whose viewer triggers the network clause), the one permissive ONNX candidate is a
whole-image scene classifier whose training data is unnamed and whose reported metrics are not
stated to come from a held-out split, and several carry no licence at all. ADR 0015 records each
candidate and its disqualification.

**Why not a colour heuristic.** Fire is orange and flickers, so a hue-and-flicker score is an
afternoon's work and would fire on sunsets, brake lights, hi-vis jackets, sodium street lighting
and a television. Presenting that as fire detection would be the exact failure this
architecture is built to prevent: a control that appears to work (README rule 1). An honest
UNAVAILABLE is more useful to an operator than a detector they must learn to disbelieve.

`observe()` **raises** rather than returning no observations. The two are not equivalent: an
empty sequence is indistinguishable from "no fire in this frame", which would be a claim this
module has no means to make. The capability gate means it is never called; if a future change to
the ledger ever did call it, failing loudly and being quarantined is the correct outcome.
"""

from __future__ import annotations

from collections.abc import Sequence

from vigil.core.capabilities import HOW_TO_PROVIDE, Capability
from vigil.core.errors import CapabilityError
from vigil.core.protocols.module import ModuleContext, ModuleDescriptor
from vigil.domain.enums import EventType
from vigil.domain.observation import Observation
from vigil.domain.scene import SceneState

MODULE_ID = "fire"

MODULE = ModuleDescriptor(
    module_id=MODULE_ID,
    event_type=EventType.FIRE,
    # 0.0.0 deliberately: there is no behaviour to version. A 1.x would imply something shipped
    # and settled, and the dismissal-rate-per-module-version metric would attribute operator
    # judgements to an algorithm that never ran.
    version="0.0.0",
    requires=frozenset({Capability.CLASSIFICATION}),
    optional=frozenset(),
    # No parameters, because no algorithm has any. A params.py of plausible-looking thresholds
    # would be the first step towards a module that looks implemented.
    default_params={},
    description=(
        "NOT IMPLEMENTED. Fire detection needs a classifier, and no fire model with a "
        "compatible licence, checkable training-data provenance and a published held-out "
        "evaluation could be sourced (ADR 0015). This module declares the requirement so the "
        "gap is reported rather than silent; it produces no observations and never runs. No "
        "colour or flicker heuristic is shipped in its place."
    ),
)


class FireModule:
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
                "fire has no detector; it should be UNAVAILABLE on every camera because "
                "nothing grants classification"
            ),
        )

    def observe(self, scene: SceneState) -> Sequence[Observation]:
        raise CapabilityError(
            f"{MODULE_ID} has no detector and cannot observe: returning no observations would "
            f"be indistinguishable from reporting no fire. Needs "
            f"{Capability.CLASSIFICATION.value}: {HOW_TO_PROVIDE[Capability.CLASSIFICATION]}",
            context={"module_id": MODULE_ID, "camera_id": self._camera_id},
        )

    def release(self) -> None:
        self._camera_id = ""


def create() -> FireModule:
    return FireModule()
