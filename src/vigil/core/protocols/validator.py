"""Event validator interface (04 §3.6, 06 §6). Implementations arrive in P3."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.domain.camera import CameraHealth
    from vigil.domain.event import CandidateEvent, ValidatorVerdict
    from vigil.domain.scene import SceneState


@dataclass(frozen=True, slots=True)
class VerificationContext:
    now_monotonic_ns: int
    camera_health: CameraHealth | None
    scene: SceneState | None


class EventValidator(Protocol):
    """PASS means 'no objection', never 'confident'. Only accumulation can raise confidence."""

    @property
    def validator_id(self) -> str: ...

    def validate(self, candidate: CandidateEvent, ctx: VerificationContext) -> ValidatorVerdict: ...
