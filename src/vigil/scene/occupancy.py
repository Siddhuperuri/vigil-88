"""Per-zone occupancy, enter/exit diffing and dwell (04 §3.3, 05 §4).

Enter and exit are computed **here, once**, by diffing against the previous frame's membership —
never by each module separately. Every module that cares about zone transitions then gets the
same answer, and the awkward cases are handled in one place:

  * a track that simply vanishes while inside a zone (lost, terminated, or evicted) appears in
    `exited_track_ids`, because it is no longer a member;
  * a track that reappears is an entry, which is what the operator saw;
  * a track that crosses a thin zone between two analysed frames is never reported as inside,
    which is honest: nothing observed it there.

Dwell is accumulated only between consecutive frames in which the track was already a member,
so entry reads 0 ms rather than one frame interval. It is kept per `(track, zone)` outside the
trajectory, because an object may sit in a zone for far longer than the bounded history window
can hold — which is exactly the case `abandoned_object` is built on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from vigil.config.schema.modules import SceneConfig
from vigil.core.units import ns_to_ms
from vigil.domain.enums import ObjectClass
from vigil.domain.scene import ZoneOccupancy
from vigil.domain.track import TrackedObject
from vigil.scene.zones import ZoneGeometry, anchor_point

PX2_PER_KPX2 = 1000.0


@dataclass(frozen=True, slots=True)
class OccupancyResult:
    occupancy: Mapping[str, ZoneOccupancy]
    # zone_dwell_ms for each track, ready to be copied onto the TrackedObject the scene reports.
    dwell_ms_by_track: Mapping[int, Mapping[str, float]]
    members_by_track: Mapping[int, frozenset[str]]


class OccupancyTracker:
    """One per camera. Holds the previous frame's membership and the dwell table."""

    __slots__ = ("_dwell_ms", "_last_t_ns", "_members")

    def __init__(self) -> None:
        self._members: dict[str, frozenset[int]] = {}
        self._dwell_ms: dict[int, dict[str, float]] = {}
        self._last_t_ns: int | None = None

    def reset(self) -> None:
        """Called on an epoch change: track ids no longer mean what they meant, so membership
        and dwell keyed by them would attribute one object's history to another."""
        self._members.clear()
        self._dwell_ms.clear()
        self._last_t_ns = None

    def update(
        self,
        tracks: Sequence[TrackedObject],
        geometry: ZoneGeometry,
        config: SceneConfig,
        *,
        t_monotonic_ns: int,
    ) -> OccupancyResult:
        dt_ms = (
            0.0
            if self._last_t_ns is None or t_monotonic_ns <= self._last_t_ns
            else ns_to_ms(t_monotonic_ns - self._last_t_ns)
        )
        inside_by_zone: dict[str, set[int]] = {z.zone_id: set() for z in geometry.zones}
        members_by_track: dict[int, set[str]] = {}
        counts: dict[str, dict[ObjectClass, int]] = {z.zone_id: {} for z in geometry.zones}

        for track in tracks:
            point = anchor_point(track, config)
            for zone in geometry.zones:
                if not geometry.contains(zone.zone_id, point):
                    continue
                inside_by_zone[zone.zone_id].add(track.track_id)
                members_by_track.setdefault(track.track_id, set()).add(zone.zone_id)
                by_class = counts[zone.zone_id]
                by_class[track.object_class] = by_class.get(track.object_class, 0) + 1

        self._accumulate_dwell(inside_by_zone, dt_ms)
        occupancy = self._build(inside_by_zone, counts, geometry)
        self._members = {zid: frozenset(ids) for zid, ids in inside_by_zone.items()}
        self._last_t_ns = t_monotonic_ns
        self._forget_absent({t.track_id for t in tracks})

        return OccupancyResult(
            occupancy=occupancy,
            dwell_ms_by_track={tid: dict(z) for tid, z in self._dwell_ms.items()},
            members_by_track={tid: frozenset(z) for tid, z in members_by_track.items()},
        )

    def _accumulate_dwell(self, inside_by_zone: Mapping[str, set[int]], dt_ms: float) -> None:
        for zone_id, inside in inside_by_zone.items():
            previous = self._members.get(zone_id, frozenset())
            for track_id in inside:
                zones = self._dwell_ms.setdefault(track_id, {})
                zones[zone_id] = (zones.get(zone_id, 0.0) + dt_ms) if track_id in previous else 0.0
            for track_id in previous - inside:
                departed = self._dwell_ms.get(track_id)
                if departed is not None:
                    departed.pop(zone_id, None)

    def _build(
        self,
        inside_by_zone: Mapping[str, set[int]],
        counts: Mapping[str, Mapping[ObjectClass, int]],
        geometry: ZoneGeometry,
    ) -> dict[str, ZoneOccupancy]:
        result: dict[str, ZoneOccupancy] = {}
        for zone in geometry.zones:
            inside = inside_by_zone[zone.zone_id]
            previous = self._members.get(zone.zone_id, frozenset())
            area_px2 = geometry.area_px2(zone.zone_id)
            density = len(inside) / (area_px2 / PX2_PER_KPX2) if area_px2 > 0.0 else 0.0
            result[zone.zone_id] = ZoneOccupancy(
                zone_id=zone.zone_id,
                counts_by_class=dict(counts[zone.zone_id]),
                track_ids=frozenset(inside),
                density_per_kpx2=density,
                entered_track_ids=frozenset(inside) - previous,
                exited_track_ids=previous - frozenset(inside),
            )
        return result

    def _forget_absent(self, live_track_ids: set[int]) -> None:
        """Dwell for a track that no longer exists is a slow leak, one subject at a time."""
        for track_id in [tid for tid in self._dwell_ms if tid not in live_track_ids]:
            del self._dwell_ms[track_id]
