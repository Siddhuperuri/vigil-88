"""Assembling the `SceneState` one analysed frame produces (04 §3.3, 05 §4).

This is the boundary every detection module sees. What it guarantees:

  * **tracks carry their zone dwell.** `tracking/` cannot compute dwell because it must not
    know about zones (01 §5), so the analyzer copies the occupancy tracker's answer onto the
    `TrackedObject` it reports. A module reads `track.zone_dwell_ms` and never recomputes it;
  * **one answer per fact.** Occupancy, enter/exit, relations and stability are computed here,
    once, so two modules can never disagree about the same scene (05 §4);
  * **missing facts are declared, not faked.** Anything that could not be computed appears in
    `degraded_facts`, and a module that needs it says nothing rather than guessing. That is why
    `SceneFact` exists: the alternative is a zero that reads as a measurement.

`capabilities` is what this camera's pipeline can actually supply for this frame, which is what
makes module availability honest (D-008): a camera with no zone file does not get `ZONES`, so
`intrusion` is reported UNAVAILABLE with the reason, rather than running and finding nothing.

One instance per camera, on that camera's analysis thread (D-005). No locks, no clock reads:
every timestamp comes from the frame.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Protocol

from vigil.config.schema.modules import SceneConfig
from vigil.core.capabilities import Capability
from vigil.core.protocols.pixels import PixelBuffer
from vigil.domain.enums import SceneFact
from vigil.domain.frame import FrameMeta
from vigil.domain.scene import SceneState, Zone
from vigil.domain.track import TrackedObject
from vigil.scene.occupancy import OccupancyTracker
from vigil.scene.relations import compute_relations
from vigil.scene.zones import ZoneGeometry

FULLY_STABLE = 1.0


class StabilityProbe(Protocol):
    """A whole-frame change measurement. `measure` returns None when it cannot be computed.

    Declared here, next to its only consumer, rather than in `core/protocols/`: it is a seam
    between two files in this engine, not a contract any other layer speaks.
    """

    def measure(self, pixels: PixelBuffer) -> float | None: ...

    def reset(self) -> None: ...


class SceneAnalyzer:
    """Implements `core.protocols.scene.SceneAnalyzer`, plus the pixel-dependent stability pass."""

    __slots__ = ("_camera_id", "_config", "_geometry", "_occupancy", "_stability", "_zones")

    def __init__(
        self,
        *,
        camera_id: str,
        config: SceneConfig,
        zones: Sequence[Zone] = (),
        stability: StabilityProbe | None = None,
    ) -> None:
        self._camera_id = camera_id
        self._config = config
        self._zones = tuple(zones)
        self._stability = stability
        self._occupancy = OccupancyTracker()
        self._geometry: ZoneGeometry | None = None

    @property
    def zones(self) -> tuple[Zone, ...]:
        return self._zones

    @property
    def has_zones(self) -> bool:
        return bool(self._zones)

    def set_zones(self, zones: Sequence[Zone]) -> None:
        """Zone edits take effect without a restart (08 §11), so this is a live swap.

        Occupancy history is dropped: membership of a zone that has just been redrawn is not
        comparable with membership of the old one, and diffing across the edit would report
        entries and exits nobody made.
        """
        self._zones = tuple(zones)
        self._geometry = None
        self._occupancy.reset()

    def reset(self) -> None:
        """Called when the tracker epoch changes: identities no longer mean what they meant."""
        self._occupancy.reset()
        if self._stability is not None:
            self._stability.reset()

    # ---------------------------------------------------------------- the pass

    def analyze(
        self,
        tracks: Sequence[TrackedObject],
        frame_meta: FrameMeta,
        zones: Sequence[Zone] | None = None,
        *,
        pixels: PixelBuffer | None = None,
        capabilities: frozenset[Capability] = frozenset(),
    ) -> SceneState:
        if zones is not None:
            self.set_zones(zones)
        geometry = self._geometry_for(frame_meta)
        degraded: set[SceneFact] = set()

        occupancy = self._occupancy.update(
            tracks, geometry, self._config, t_monotonic_ns=frame_meta.t_monotonic_ns
        )
        if not geometry.zones:
            degraded.add(SceneFact.ZONE_OCCUPANCY)

        enriched = tuple(
            self._with_dwell(t, occupancy.dwell_ms_by_track.get(t.track_id, {})) for t in tracks
        )
        relations = compute_relations(enriched, geometry, self._config)
        if relations.degraded:
            degraded.add(SceneFact.SPATIAL_RELATIONS)

        stability = self._measure_stability(pixels)
        if stability is None:
            degraded.add(SceneFact.STABILITY)
        if not any(t.velocity_px_s is not None for t in enriched) and enriched:
            degraded.add(SceneFact.VELOCITY)

        return SceneState(
            frame_meta=frame_meta,
            tracks=enriched,
            zones=geometry.zones,
            occupancy=occupancy.occupancy,
            relations=relations.relations,
            stability_ratio=FULLY_STABLE if stability is None else stability,
            capabilities=capabilities,
            degraded_facts=frozenset(degraded),
        )

    def _geometry_for(self, frame_meta: FrameMeta) -> ZoneGeometry:
        geometry = self._geometry
        if geometry is None or not geometry.matches(frame_meta.width_px, frame_meta.height_px):
            geometry = ZoneGeometry(
                self._zones, width_px=frame_meta.width_px, height_px=frame_meta.height_px
            )
            self._geometry = geometry
        return geometry

    def _measure_stability(self, pixels: PixelBuffer | None) -> float | None:
        if self._stability is None or pixels is None or not self._config.stability_enabled:
            return None
        return self._stability.measure(pixels)

    @staticmethod
    def _with_dwell(track: TrackedObject, dwell_ms: Mapping[str, float]) -> TrackedObject:
        """`TrackedObject` is frozen, so this returns a new one with the dwell map filled in."""
        return track if not dwell_ms else replace(track, zone_dwell_ms=dwell_ms)
