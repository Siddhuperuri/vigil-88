"""Scene analyzer interface (04 §3.3). Implementation arrives in P2."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.domain.frame import FrameMeta
    from vigil.domain.scene import SceneState, Zone
    from vigil.domain.track import TrackedObject


class SceneAnalyzer(Protocol):
    def analyze(
        self,
        tracks: Sequence[TrackedObject],
        frame_meta: FrameMeta,
        zones: Sequence[Zone],
    ) -> SceneState: ...
