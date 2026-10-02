"""Tracker interface (04 §3.2). Implementation arrives in P2."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.domain.detection import DetectionSet
    from vigil.domain.frame import FrameMeta
    from vigil.domain.track import TrackedObject


class Tracker(Protocol):
    """One instance per camera, owned by exactly one thread (D-005)."""

    @property
    def epoch(self) -> int: ...

    def update(
        self, detections: DetectionSet, frame_meta: FrameMeta
    ) -> Sequence[TrackedObject]: ...

    def reset(self) -> None:
        """Bumps `epoch`: every downstream consumer treats it as 'all identities are new'."""
        ...
