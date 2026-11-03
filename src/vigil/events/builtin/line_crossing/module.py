"""The line-crossing module: a tracked object crossed a tripwire in the direction that counts.

It reads the track's own trajectory, so it keeps no state of its own and decides nothing about
whether an incident occurred (D-003): a crossing is one hypothesis-supporting fact, reported on
every frame for `lookback_ms` after it happens so that accumulation has something to accumulate.

**Geometry only, and unevaluated.** There is no model here. The module is exactly as good as the
detector and tracker beneath it, and it has been tested on synthetic scenes only, not on real
footage. A line drawn across a doorway is not a claim about who went through it.

The event type is `RESTRICTED_AREA_ENTRY`: crossing a boundary in a forbidden direction is the
narrower sibling of an intrusion, and arbitration already suppresses it when an `INTRUSION` is
active for the same subject (06 §7).
"""

from __future__ import annotations

from collections.abc import Sequence

from vigil.core.capabilities import Capability
from vigil.core.geometry import Point, crossing_direction, side_of_line
from vigil.core.protocols.module import ModuleContext, ModuleDescriptor
from vigil.domain.enums import CrossingDirection, EventType, ObjectClass
from vigil.domain.observation import FactValue, Observation, SubjectKey
from vigil.domain.scene import CrossingLine, SceneState
from vigil.domain.track import TrackedObject
from vigil.events.builtin.line_crossing.params import LineCrossingParams

MODULE_ID = "line_crossing"

MODULE = ModuleDescriptor(
    module_id=MODULE_ID,
    event_type=EventType.RESTRICTED_AREA_ENTRY,
    version="1.0.0",
    requires=frozenset({Capability.DETECTION, Capability.TRACKING, Capability.ZONES}),
    optional=frozenset({Capability.VELOCITY}),
    default_params={
        "object_classes": [ObjectClass.PERSON.value],
        "lookback_ms": 4000.0,
        "min_track_hits": 3,
        "min_hit_ratio": 0.4,
        "base_signal": 0.72,
        "velocity_bonus_ratio": 0.10,
        "allow_interpolated": False,
    },
    description="A tracked object crossed a tripwire in the direction that counts.",
)

_MIN_SIGNAL = 0.51
_MAX_SIGNAL = 0.95
_NS_PER_MS = 1_000_000


class LineCrossingModule:
    __slots__ = ("_params",)

    def __init__(self) -> None:
        self._params: LineCrossingParams | None = None

    @property
    def descriptor(self) -> ModuleDescriptor:
        return MODULE

    def bind(self, ctx: ModuleContext) -> None:
        self._params = LineCrossingParams.model_validate(dict(ctx.params))

    def release(self) -> None:
        return None

    @property
    def params(self) -> LineCrossingParams:
        if self._params is None:
            raise RuntimeError("LineCrossingModule.observe() called before bind()")
        return self._params

    def observe(self, scene: SceneState) -> Sequence[Observation]:
        out: list[Observation] = []
        for line in scene.lines:
            if not line.is_active_at(scene.frame_meta.wall_utc):
                continue
            for track in scene.tracks:
                observation = self._observe_one(scene, line, track)
                if observation is not None:
                    out.append(observation)
        return tuple(out)

    def _observe_one(
        self, scene: SceneState, line: CrossingLine, track: TrackedObject
    ) -> Observation | None:
        params = self.params
        if track.object_class not in params.object_classes:
            return None
        if track.hits < params.min_track_hits or track.hit_ratio < params.min_hit_ratio:
            return None
        crossed = self._latest_crossing(scene, line, track)
        if crossed is None:
            return None
        ago_ms, direction = crossed
        signal = params.base_signal
        facts: dict[str, FactValue] = {
            "line_id": line.line_id,
            "line_direction": line.direction.value,
            "crossed_direction": "to_right" if direction > 0 else "to_left",
            "crossed_ago_ms": round(ago_ms, 1),
            "line_criticality": float(line.criticality),
            "track_id": float(track.track_id),
            "object_class": track.object_class.value,
            "hits": float(track.hits),
            "hit_ratio": round(track.hit_ratio, 4),
            "base_signal": params.base_signal,
        }
        if Capability.VELOCITY in scene.capabilities and params.velocity_bonus_ratio > 0.0:
            if self._still_heading_that_way(scene, line, track, direction):
                signal += params.velocity_bonus_ratio
                facts["velocity_bonus"] = params.velocity_bonus_ratio
        else:
            facts["velocity_unavailable"] = True
        clamped = min(max(signal, _MIN_SIGNAL), _MAX_SIGNAL)
        facts["signal"] = round(clamped, 4)
        return Observation(
            camera_id=scene.frame_meta.camera_id,
            module_id=MODULE_ID,
            subject=SubjectKey("track", str(track.track_id)),
            signal=clamped,
            t_monotonic_ns=scene.frame_meta.t_monotonic_ns,
            facts=facts,
        )

    # ---------------------------------------------------------------- geometry

    @staticmethod
    def _line_px(scene: SceneState, line: CrossingLine) -> tuple[Point, Point]:
        width, height = scene.frame_meta.width_px, scene.frame_meta.height_px
        return (
            Point(line.start.x * width, line.start.y * height),
            Point(line.end.x * width, line.end.y * height),
        )

    def _latest_crossing(
        self, scene: SceneState, line: CrossingLine, track: TrackedObject
    ) -> tuple[float, int] | None:
        """The most recent counting crossing inside the lookback, as (ms ago, +1 or -1)."""
        params = self.params
        a, b = self._line_px(scene, line)
        horizon = scene.frame_meta.t_monotonic_ns - int(params.lookback_ms * _NS_PER_MS)
        found: tuple[float, int] | None = None
        previous: tuple[Point, int] | None = None  # the last point whose side was certain
        for point in track.trajectory.points:
            if point.is_interpolated and not params.allow_interpolated:
                previous = None  # a guess breaks the chain: do not bridge across it
                continue
            here = Point((point.bbox.x1 + point.bbox.x2) / 2.0, point.bbox.y2)  # the foot point
            side = side_of_line(a, b, here)
            if side == 0.0:
                continue
            sign = 1 if side > 0 else -1
            if previous is not None and previous[1] != sign:
                direction = crossing_direction(a, b, previous[0], here)
                counted = direction != 0 and self._counts(line.direction, direction)
                if counted and point.t_monotonic_ns >= horizon:
                    ago = (scene.frame_meta.t_monotonic_ns - point.t_monotonic_ns) / _NS_PER_MS
                    found = (ago, direction)
            previous = (here, sign)
        return found

    @staticmethod
    def _counts(wanted: CrossingDirection, direction: int) -> bool:
        if wanted is CrossingDirection.ANY:
            return True
        return (wanted is CrossingDirection.TO_RIGHT) == (direction > 0)

    def _still_heading_that_way(
        self, scene: SceneState, line: CrossingLine, track: TrackedObject, direction: int
    ) -> bool:
        velocity = track.velocity_px_s
        if velocity is None or velocity.magnitude == 0.0:
            return False
        a, b = self._line_px(scene, line)
        # Velocity across the line: positive when heading for the line's positive side.
        across = (b.x - a.x) * velocity.y - (b.y - a.y) * velocity.x
        return across != 0.0 and (across > 0) == (direction > 0)


def create() -> LineCrossingModule:
    return LineCrossingModule()
