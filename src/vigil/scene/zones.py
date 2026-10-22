"""Zones: config to domain, and the containment rule (04 §3.3, 02 §2 `scene/zones.py`).

Polygons are stored normalized in [0, 1] and evaluated in pixels, so a resolution change — or
dropping an inference tier under load — never invalidates a zone an operator drew.

**Containment is decided by one anchor point, not by box overlap.** A person standing outside a
fence still has a box that overlaps the yard behind them, because a box is a rectangle and a
person is not; using overlap would put every passer-by inside the zone. The anchor is the foot
point (bottom centre) for anything that rests on the ground and the centre for anything that
does not — flames and smoke have no footprint — which is what `scene.zone_anchor` and
`scene.center_anchor_classes` select. The intrusion module applies the same rule to its own
boundary-distance term, and the two must agree or they will disagree at a boundary.
"""

from __future__ import annotations

from collections.abc import Sequence

from vigil.config.schema.modules import SceneConfig
from vigil.config.schema.zones import ScheduleConfig, ZoneConfig, ZoneSetConfig, parse_clock
from vigil.core.geometry import Point, Polygon
from vigil.domain.enums import ObjectClass, ZoneKind
from vigil.domain.scene import Schedule, Zone
from vigil.domain.track import TrackedObject


def schedule_from_config(config: ScheduleConfig) -> Schedule:
    return Schedule(
        start_minute=parse_clock(config.start),
        end_minute=parse_clock(config.end),
        utc_offset_minutes=config.utc_offset_minutes,
        weekdays=frozenset(config.weekdays),
    )


def zone_from_config(config: ZoneConfig, camera_id: str) -> Zone:
    return Zone(
        zone_id=config.zone_id,
        camera_id=camera_id,
        kind=ZoneKind(config.kind),
        polygon=config.to_polygon(),
        criticality=config.criticality,
        active_schedule=(
            None if config.schedule is None else schedule_from_config(config.schedule)
        ),
        label=config.display_label,
    )


def zones_from_set(zone_set: ZoneSetConfig, camera_id: str, *, max_zones: int) -> tuple[Zone, ...]:
    """Materialise a validated zone file for one camera.

    The cap is enforced here rather than in the schema because it is a runtime cost bound
    (occupancy is linear in zones x tracks), not a property of the file format.
    """
    zones = tuple(zone_from_config(z, camera_id) for z in zone_set.zones[:max_zones])
    return zones


class ZoneGeometry:
    """Pixel-space geometry for one camera's zones at one frame size.

    Built once per (camera, frame size) and reused for every frame, because converting
    normalized polygons to pixels on every frame for every track is pure waste at 10-30 fps.
    """

    __slots__ = ("_height_px", "_polygons", "_width_px", "_zones")

    def __init__(self, zones: Sequence[Zone], *, width_px: int, height_px: int) -> None:
        self._zones = tuple(zones)
        self._width_px = width_px
        self._height_px = height_px
        self._polygons = {
            z.zone_id: Polygon([Point(p.x * width_px, p.y * height_px) for p in z.polygon.points])
            for z in self._zones
        }

    @property
    def zones(self) -> tuple[Zone, ...]:
        return self._zones

    def matches(self, width_px: int, height_px: int) -> bool:
        return self._width_px == width_px and self._height_px == height_px

    def polygon_px(self, zone_id: str) -> Polygon | None:
        return self._polygons.get(zone_id)

    def contains(self, zone_id: str, point: Point) -> bool:
        polygon = self._polygons.get(zone_id)
        return polygon is not None and polygon.contains(point)

    def distance_to_boundary_px(self, zone_id: str, point: Point) -> float | None:
        polygon = self._polygons.get(zone_id)
        return None if polygon is None else polygon.distance_to_boundary(point)

    def signed_distance_px(self, zone_id: str, point: Point) -> float | None:
        """Positive outside the zone, negative inside; the magnitude is the clearance.

        The unsigned distance is the wrong tool for "is this subject closing on the zone": it
        starts growing again the moment the point crosses the edge, so a subject walking
        straight in reads as moving *away*. Signing it makes the comparison monotonic.
        """
        polygon = self._polygons.get(zone_id)
        if polygon is None:
            return None
        clearance = polygon.distance_to_boundary(point)
        return -clearance if polygon.contains(point) else clearance

    def area_px2(self, zone_id: str) -> float:
        polygon = self._polygons.get(zone_id)
        return 0.0 if polygon is None else polygon.area()


def anchor_point(track: TrackedObject, config: SceneConfig) -> Point:
    """Where the object meets the scene: the point that decides zone containment."""
    centre_classes = {c.strip().lower() for c in config.center_anchor_classes}
    use_centre = config.zone_anchor == "center" or _class_name(track.object_class) in centre_classes
    return track.bbox.center if use_centre else track.bbox.bottom_center


def _class_name(object_class: ObjectClass) -> str:
    return object_class.value.lower()
