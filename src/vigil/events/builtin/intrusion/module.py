"""The intrusion module: a tracked person inside an active restricted zone (06 §4, §11).

This is the module that validates the platform rather than the one that stresses the AI. Ground
truth is unambiguous — either a person was in the polygon or they were not — so when the system
gets it wrong, the fault is in the platform and not in perception.

What this module does **not** do, and the list is the point:

  * it does not decide that an intrusion occurred. "Person enters restricted zone" is not
    "confirmed intrusion"; it is one frame's support for that hypothesis (D-003);
  * it keeps no timers, no counters of consecutive frames, no "has this already fired";
  * it creates no incident, writes no evidence, sends no alert;
  * it reads no clock, no filesystem, no network, no global state.

`observe()` is a pure function of the scene and the module's declared parameters. Every branch
is unit-testable with a hand-built `SceneState` and no pipeline (09 §12).

The only state it keeps is the epoch it last saw, so it can honour rule 4; that state is one
integer and is reset by `bind()`.
"""

from __future__ import annotations

from collections.abc import Sequence

from vigil.core.capabilities import Capability
from vigil.core.geometry import Point
from vigil.core.protocols.module import ModuleContext, ModuleDescriptor
from vigil.domain.enums import EventType, ObjectClass, SceneFact, TrackFlag, ZoneKind
from vigil.domain.observation import FactValue, Observation, SubjectKey
from vigil.domain.scene import SceneState, Zone
from vigil.domain.track import TrackedObject
from vigil.events.builtin.intrusion.params import IntrusionParams

MODULE_ID = "intrusion"

MODULE = ModuleDescriptor(
    module_id=MODULE_ID,
    event_type=EventType.INTRUSION,
    version="1.0.0",
    requires=frozenset({Capability.DETECTION, Capability.TRACKING, Capability.ZONES}),
    optional=frozenset({Capability.VELOCITY}),
    default_params={
        "object_classes": [ObjectClass.PERSON.value],
        "zone_kinds": [ZoneKind.RESTRICTED.value],
        "min_track_hits": 3,
        "base_signal": 0.70,
        "velocity_bonus_ratio": 0.15,
        "edge_margin_ratio": 0.02,
        "edge_penalty_ratio": 0.15,
        "dwell_bonus_after_ms": 3000.0,
        "dwell_bonus_ratio": 0.10,
        "min_hit_ratio": 0.4,
    },
    description="A tracked person present inside a restricted zone while that zone is active.",
)

# Signals are clamped into a band strictly inside (0, 1). The ceiling keeps one frame from
# dominating accumulation; the floor keeps a penalised-but-real presence from reading as
# evidence *against* an intrusion, which it is not.
_MIN_SIGNAL = 0.51
_MAX_SIGNAL = 0.95


class IntrusionModule:
    """One instance per camera, bound once at startup."""

    __slots__ = ("_epoch", "_params")

    def __init__(self) -> None:
        self._params: IntrusionParams | None = None
        self._epoch: int | None = None

    @property
    def descriptor(self) -> ModuleDescriptor:
        return MODULE

    def bind(self, ctx: ModuleContext) -> None:
        """Validate parameters against this module's own model. An unknown key is an error."""
        self._params = IntrusionParams.model_validate(dict(ctx.params))
        self._epoch = None

    def release(self) -> None:
        self._epoch = None

    @property
    def params(self) -> IntrusionParams:
        if self._params is None:
            raise RuntimeError("IntrusionModule.observe() called before bind()")
        return self._params

    # ---------------------------------------------------------------- observe

    def observe(self, scene: SceneState) -> Sequence[Observation]:
        params = self.params
        if self._epoch != scene.frame_meta.stream_epoch:
            self._epoch = scene.frame_meta.stream_epoch  # rule 4: bounded state, reset on epoch
        if SceneFact.ZONE_OCCUPANCY in scene.degraded_facts:
            # Occupancy could not be computed this frame. A guess would be worse than silence:
            # the module declares what it needs and says nothing when it is absent.
            return ()

        out: list[Observation] = []
        for zone in scene.zones:
            if zone.kind not in params.zone_kinds:
                continue
            if not zone.is_active_at(scene.frame_meta.wall_utc):
                continue
            occupancy = scene.occupancy.get(zone.zone_id)
            if occupancy is None:
                continue
            for track_id in sorted(occupancy.track_ids):
                observation = self._observe_one(scene, zone, track_id)
                if observation is not None:
                    out.append(observation)
        return tuple(out)

    def _observe_one(self, scene: SceneState, zone: Zone, track_id: int) -> Observation | None:
        params = self.params
        try:
            track = scene.track_by_id(track_id)
        except KeyError:
            # Occupancy named a track the scene does not carry. Reporting on an object we
            # cannot describe would produce an unexplainable event.
            return None
        if track.object_class not in params.object_classes:
            return None
        if track.hits < params.min_track_hits:
            return None
        if track.hit_ratio < params.min_hit_ratio:
            return None
        if TrackFlag.NEW in track.flags:
            return None

        signal, facts = self._score(scene, zone, track)
        return Observation(
            camera_id=scene.frame_meta.camera_id,
            module_id=MODULE_ID,
            subject=SubjectKey("track", str(track.track_id)),
            signal=signal,
            t_monotonic_ns=scene.frame_meta.t_monotonic_ns,
            facts=facts,
        )

    def _score(
        self, scene: SceneState, zone: Zone, track: TrackedObject
    ) -> tuple[float, dict[str, FactValue]]:
        """Build this frame's signal, recording every term that contributed.

        `facts` is not decoration: it becomes the explanation an operator reads and the fixture
        a regression test asserts against (06 §4).
        """
        params = self.params
        signal = params.base_signal
        facts: dict[str, FactValue] = {
            "zone_id": zone.zone_id,
            "zone_kind": zone.kind.value,
            "zone_criticality": float(zone.criticality),
            "track_id": float(track.track_id),
            "object_class": track.object_class.value,
            "hits": float(track.hits),
            "hit_ratio": round(track.hit_ratio, 4),
            "base_signal": params.base_signal,
        }

        dwell_ms = track.zone_dwell_ms.get(zone.zone_id, 0.0)
        facts["dwell_ms"] = round(dwell_ms, 1)
        if dwell_ms >= params.dwell_bonus_after_ms and params.dwell_bonus_ratio > 0.0:
            signal += params.dwell_bonus_ratio
            facts["dwell_bonus"] = params.dwell_bonus_ratio

        margin_px = params.edge_margin_ratio * min(
            scene.frame_meta.width_px, scene.frame_meta.height_px
        )
        distance_px = self._distance_to_boundary_px(scene, zone, track)
        if distance_px is not None:
            facts["boundary_distance_px"] = round(distance_px, 2)
            if distance_px < margin_px and params.edge_penalty_ratio > 0.0:
                signal -= params.edge_penalty_ratio
                facts["edge_penalty"] = params.edge_penalty_ratio

        if Capability.VELOCITY in scene.capabilities and params.velocity_bonus_ratio > 0.0:
            inwardness = self._inwardness(scene, zone, track)
            if inwardness is not None:
                bonus = params.velocity_bonus_ratio * inwardness
                signal += bonus
                facts["inwardness"] = round(inwardness, 4)
                facts["velocity_bonus"] = round(bonus, 4)
        else:
            facts["velocity_unavailable"] = True

        clamped = min(max(signal, _MIN_SIGNAL), _MAX_SIGNAL)
        facts["signal"] = round(clamped, 4)
        return (clamped, facts)

    # ---------------------------------------------------------------- geometry

    @staticmethod
    def _anchor_px(scene: SceneState, track: TrackedObject) -> Point:
        """Where the object touches the scene. A person stands on the ground, so the foot point
        is what decides containment; the scene analyzer uses the same rule (`scene.zone_anchor`),
        and this module must agree with it or the two will disagree at a boundary."""
        box = track.bbox
        return Point((box.x1 + box.x2) / 2.0, box.y2)

    @staticmethod
    def _polygon_px(scene: SceneState, zone: Zone) -> list[Point]:
        width, height = scene.frame_meta.width_px, scene.frame_meta.height_px
        return [Point(p.x * width, p.y * height) for p in zone.polygon.points]

    def _distance_to_boundary_px(
        self, scene: SceneState, zone: Zone, track: TrackedObject
    ) -> float | None:
        from vigil.core.geometry import Polygon

        points = self._polygon_px(scene, zone)
        if len(points) < 3:
            return None
        return Polygon(points).distance_to_boundary(self._anchor_px(scene, track))

    def _inwardness(self, scene: SceneState, zone: Zone, track: TrackedObject) -> float | None:
        """How much of the subject's motion is directed away from the boundary, in [0, 1].

        Measured as the drop in distance-to-boundary that one second of the current velocity
        would produce, normalised by the distance travelled. Moving deeper into the zone scores
        near 1; moving toward the exit scores 0. A stationary subject has no direction, so it
        earns no bonus rather than a misleading one.
        """
        from vigil.core.geometry import Polygon

        velocity = track.velocity_px_s
        if velocity is None or velocity.magnitude == 0.0:
            return None
        if TrackFlag.STATIONARY in track.flags:
            return None
        points = self._polygon_px(scene, zone)
        if len(points) < 3:
            return None
        polygon = Polygon(points)
        here = self._anchor_px(scene, track)
        ahead = Point(here.x + velocity.x, here.y + velocity.y)
        travelled = velocity.magnitude
        if travelled == 0.0:
            return None
        gain = polygon.distance_to_boundary(ahead) - polygon.distance_to_boundary(here)
        if not polygon.contains(ahead):
            return 0.0  # a step that leaves the zone is not inward, whatever the arithmetic says
        return min(max(gain / travelled, 0.0), 1.0)


def create() -> IntrusionModule:
    """The factory the registry discovers (`events/registry.py`)."""
    return IntrusionModule()
