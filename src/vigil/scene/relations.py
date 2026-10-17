"""Spatial relations between tracks, and between a track and a zone (04 §3.3, 05 §4).

Four kinds, each a fact a module would otherwise have to derive for itself — and derive
slightly differently, which is how two modules come to disagree about the same scene:

  INSIDE       the subject's anchor is within the zone; `distance_px` is its clearance
  NEAR         two boxes whose gap is small relative to their own size
  APPROACHING  the subject is outside a zone, close to it, and closing on it
  CONVERGING   two tracks whose separation is shrinking

Every threshold is relative to the objects' own box heights rather than in pixels, because a
pixel distance silently means different things at 640x480 and at 1920x1080, and different things
again for a subject near the camera and one far from it.

Only **confirmed, currently visible** tracks take part. A predicted box is the filter's
extrapolation, and a relation computed from two extrapolations is a statement about the filter
rather than about the scene.

Relations are quadratic in the track count, so above `scene.max_relation_tracks` they are
skipped and reported as a degraded fact (`SceneFact.SPATIAL_RELATIONS`) instead of being
computed late. A late answer on the analysis thread costs every other camera latency.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from vigil.config.schema.modules import SceneConfig
from vigil.core.geometry import Point
from vigil.domain.enums import SpatialRelationKind, TrackFlag
from vigil.domain.scene import SpatialRelation
from vigil.domain.track import TrackedObject
from vigil.scene.zones import ZoneGeometry, anchor_point

_MIN_HEIGHT_PX = 1.0


@dataclass(frozen=True, slots=True)
class RelationResult:
    relations: tuple[SpatialRelation, ...]
    degraded: bool  # too many eligible tracks: relations were not computed


def eligible(tracks: Sequence[TrackedObject]) -> tuple[TrackedObject, ...]:
    """Confirmed and visible, in track-id order so output never depends on arrival order."""
    chosen = [t for t in tracks if t.is_visible and TrackFlag.NEW not in t.flags]
    return tuple(sorted(chosen, key=lambda t: t.track_id))


def compute_relations(
    tracks: Sequence[TrackedObject], geometry: ZoneGeometry, config: SceneConfig
) -> RelationResult:
    subjects = eligible(tracks)
    if len(subjects) > config.max_relation_tracks:
        return RelationResult((), True)

    relations: list[SpatialRelation] = []
    for track in subjects:
        relations.extend(_zone_relations(track, geometry, config))
    for a, b in combinations(subjects, 2):
        relations.extend(_pair_relations(a, b, config))
    return RelationResult(tuple(relations), False)


def _mean_height(a: TrackedObject, b: TrackedObject) -> float:
    return max((a.bbox.height + b.bbox.height) / 2.0, _MIN_HEIGHT_PX)


def _zone_relations(
    track: TrackedObject, geometry: ZoneGeometry, config: SceneConfig
) -> list[SpatialRelation]:
    out: list[SpatialRelation] = []
    anchor = anchor_point(track, config)
    height = max(track.bbox.height, _MIN_HEIGHT_PX)
    for zone in geometry.zones:
        clearance = geometry.distance_to_boundary_px(zone.zone_id, anchor)
        if clearance is None:
            continue
        if geometry.contains(zone.zone_id, anchor):
            out.append(
                SpatialRelation(
                    kind=SpatialRelationKind.INSIDE,
                    subject_track_id=track.track_id,
                    object_track_id=None,
                    zone_id=zone.zone_id,
                    distance_px=clearance,
                )
            )
            continue
        if clearance > config.approach_max_distance_ratio * height:
            continue
        closing = _closing_speed_to_zone(track, geometry, zone.zone_id, anchor)
        if closing is None or closing / height < config.approach_min_closing_speed_ratio:
            continue
        out.append(
            SpatialRelation(
                kind=SpatialRelationKind.APPROACHING,
                subject_track_id=track.track_id,
                object_track_id=None,
                zone_id=zone.zone_id,
                distance_px=clearance,
            )
        )
    return out


def _closing_speed_to_zone(
    track: TrackedObject, geometry: ZoneGeometry, zone_id: str, anchor: Point
) -> float | None:
    """Pixels per second by which one second of the current velocity would close the gap.

    Signed distance, so a subject that would cross the edge within the second reads as closing
    fast rather than as moving away (`ZoneGeometry.signed_distance_px`).
    """
    velocity = track.velocity_px_s
    if velocity is None or velocity.magnitude == 0.0:
        return None
    here = geometry.signed_distance_px(zone_id, anchor)
    ahead = geometry.signed_distance_px(
        zone_id, Point(anchor.x + velocity.x, anchor.y + velocity.y)
    )
    if here is None or ahead is None:
        return None
    return here - ahead


def _pair_relations(
    a: TrackedObject, b: TrackedObject, config: SceneConfig
) -> list[SpatialRelation]:
    out: list[SpatialRelation] = []
    height = _mean_height(a, b)
    gap = a.bbox.gap_to(b.bbox)
    if gap <= config.near_distance_ratio * height:
        out.append(
            SpatialRelation(
                kind=SpatialRelationKind.NEAR,
                subject_track_id=a.track_id,
                object_track_id=b.track_id,
                zone_id=None,
                distance_px=gap,
            )
        )
    closing = _closing_speed_between(a, b)
    if closing is not None and closing / height >= config.approach_min_closing_speed_ratio:
        out.append(
            SpatialRelation(
                kind=SpatialRelationKind.CONVERGING,
                subject_track_id=a.track_id,
                object_track_id=b.track_id,
                zone_id=None,
                distance_px=gap,
            )
        )
    return out


def _closing_speed_between(a: TrackedObject, b: TrackedObject) -> float | None:
    """How fast the centres approach, in pixels per second. Negative means separating."""
    va, vb = a.velocity_px_s, b.velocity_px_s
    if va is None or vb is None:
        return None
    ca, cb = a.bbox.center, b.bbox.center
    here = ca.distance_to(cb)
    ahead = Point(ca.x + va.x, ca.y + va.y).distance_to(Point(cb.x + vb.x, cb.y + vb.y))
    return here - ahead
