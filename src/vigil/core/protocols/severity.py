"""Severity engine interface (06 §8). Implementation arrives in P3."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from vigil.domain.event import VerifiedEvent
    from vigil.domain.incident import SeverityAssessment


@dataclass(frozen=True, slots=True)
class SeverityContext:
    now_wall_utc: datetime
    camera_trust: float
    related_incident_count: int


class SeverityEngine(Protocol):
    """Severity never feeds back into confidence: different questions (06 §8)."""

    def score(self, event: VerifiedEvent, ctx: SeverityContext) -> SeverityAssessment: ...
