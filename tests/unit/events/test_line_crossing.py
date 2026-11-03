"""line_crossing: a crossing is read from the track's own path, and silence is the common answer."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from tests.support.scenes import FULL_CAPABILITIES, SceneStateBuilder
from vigil.core.capabilities import Capability
from vigil.core.geometry import BBox, Point
from vigil.core.protocols.module import ModuleContext
from vigil.core.units import ms_to_ns
from vigil.domain.enums import CrossingDirection, EventType, ObjectClass
from vigil.domain.observation import Observation
from vigil.domain.scene import CrossingLine, SceneState, Schedule
from vigil.domain.track import TrackPoint, Trajectory
from vigil.events.builtin.line_crossing import (
    MODULE,
    LineCrossingModule,
    LineCrossingParams,
    create,
)
from vigil.observability.logging import get_logger

CAMERA = "test-cam"
W, H = 1280, 720
# A vertical line down the middle of the frame, drawn top to bottom. Walking from its start to its
# end faces down the screen, so the walker's right hand is toward the left of the image: positive
# side = left half. "to_right" therefore means crossing from the right half into the left half.
LINE = ((0.5, 0.1), (0.5, 0.9))
Path = list[tuple[float, float, float]]  # x, y (normalized foot point), t in ms


def line(
    direction: CrossingDirection = CrossingDirection.ANY, schedule: Schedule | None = None
) -> CrossingLine:
    return CrossingLine(
        line_id="gate",
        camera_id=CAMERA,
        start=Point(*LINE[0]),
        end=Point(*LINE[1]),
        direction=direction,
        criticality=2,
        active_schedule=schedule,
        label="Gate",
    )


def module(**params: object) -> LineCrossingModule:
    built = create()
    built.bind(
        ModuleContext(
            camera_id=CAMERA,
            params={**MODULE.default_params, **params},
            logger=get_logger("test.line"),
        )
    )
    return built


def scene(
    path: Path,
    *,
    lines: tuple[CrossingLine, ...] | None = None,
    object_class: ObjectClass = ObjectClass.PERSON,
    hits: int = 10,
    interpolated: frozenset[int] = frozenset(),
    capabilities: frozenset[Capability] = FULL_CAPABILITIES,
    velocity: tuple[float, float] | None = None,
) -> SceneState:
    """A scene at the last time of `path`, with one track that walked it."""
    end_x, end_y, end_t = path[-1]
    builder = SceneStateBuilder(camera_id=CAMERA, width_px=W, height_px=H)
    builder.t_ms = end_t
    built = (
        builder.with_capabilities(capabilities)
        .with_track(1, object_class, at=(end_x, end_y), hits=hits, velocity_px_s=velocity)
        .build()
    )

    def box(x: float, y: float) -> BBox:
        foot_x, foot_y = x * W, y * H
        return BBox(foot_x - 30, foot_y - 160, foot_x + 30, foot_y)

    points = tuple(
        TrackPoint(ms_to_ns(t), box(x, y), 0.9, i in interpolated)
        for i, (x, y, t) in enumerate(path)
    )
    track = replace(built.tracks[0], trajectory=Trajectory(points, 90))
    return replace(built, tracks=(track,), lines=lines if lines is not None else (line(),))


def fired(m: LineCrossingModule, s: SceneState) -> Observation | None:
    out = m.observe(s)
    assert len(out) <= 1
    return out[0] if out else None


LEFT_TO_RIGHT: Path = [(0.4, 0.5, 0), (0.45, 0.5, 100), (0.55, 0.5, 200), (0.6, 0.5, 300)]
RIGHT_TO_LEFT: Path = [(0.6, 0.5, 0), (0.55, 0.5, 100), (0.45, 0.5, 200), (0.4, 0.5, 300)]


# ------------------------------------------------------------------ the descriptor


def test_it_is_the_narrow_sibling_of_intrusion_and_needs_zones_and_tracking() -> None:
    assert MODULE.event_type is EventType.RESTRICTED_AREA_ENTRY
    assert {Capability.DETECTION, Capability.TRACKING, Capability.ZONES} <= MODULE.requires
    assert Capability.VELOCITY in MODULE.optional


# ------------------------------------------------------------------ what it reports


def test_a_crossing_in_either_direction_is_reported_when_any_direction_counts() -> None:
    m = module()
    assert fired(m, scene(LEFT_TO_RIGHT)) is not None
    assert fired(m, scene(RIGHT_TO_LEFT)) is not None


def test_only_the_counted_direction_fires() -> None:
    m = module()
    # Moving right across the screen is a move toward the line's negative side here.
    fwd = scene(LEFT_TO_RIGHT, lines=(line(CrossingDirection.TO_LEFT),))
    back = scene(LEFT_TO_RIGHT, lines=(line(CrossingDirection.TO_RIGHT),))
    assert (fired(m, fwd) is None) != (fired(m, back) is None)


def test_the_observation_says_which_line_which_way_and_how_long_ago() -> None:
    obs = fired(module(), scene(LEFT_TO_RIGHT))
    assert obs is not None
    assert obs.module_id == "line_crossing" and obs.subject.kind == "track"
    assert obs.facts["line_id"] == "gate"
    assert obs.facts["crossed_ago_ms"] == 100.0  # the crossing happened between t=100 and t=200
    assert obs.facts["crossed_direction"] in {"to_right", "to_left"}
    assert 0.5 < obs.signal < 1.0


def test_it_keeps_reporting_until_the_lookback_runs_out() -> None:
    m = module(lookback_ms=1000.0)
    path: Path = [(0.4, 0.5, 0), (0.6, 0.5, 100)]
    later: Path = [*path, (0.62, 0.5, 600), (0.64, 0.5, 1000)]
    much_later: Path = [*path, (0.62, 0.5, 600), (0.9, 0.5, 5000)]
    assert fired(m, scene(path)) is not None
    assert fired(m, scene(later)) is not None
    assert fired(m, scene(much_later)) is None


def test_a_point_that_lands_exactly_on_the_line_does_not_hide_the_crossing() -> None:
    path: Path = [(0.4, 0.5, 0), (0.5, 0.5, 100), (0.6, 0.5, 200)]
    assert fired(module(), scene(path)) is not None


def test_the_signal_is_stronger_for_a_track_still_heading_that_way() -> None:
    m = module()
    plain = fired(m, scene(LEFT_TO_RIGHT, velocity=None))
    heading_on = fired(m, scene(LEFT_TO_RIGHT, velocity=(80.0, 0.0)))
    heading_back = fired(m, scene(LEFT_TO_RIGHT, velocity=(-80.0, 0.0)))
    assert plain and heading_on and heading_back
    assert heading_on.signal > heading_back.signal
    assert "velocity_bonus" in heading_on.facts and "velocity_bonus" not in heading_back.facts


def test_without_the_velocity_capability_it_says_so_instead_of_guessing() -> None:
    obs = fired(
        module(), scene(LEFT_TO_RIGHT, capabilities=FULL_CAPABILITIES - {Capability.VELOCITY})
    )
    assert obs is not None and obs.facts["velocity_unavailable"] is True


# ------------------------------------------------------------------ what it declines to say


def test_staying_on_one_side_is_silence() -> None:
    assert fired(module(), scene([(0.1, 0.5, 0), (0.2, 0.5, 100), (0.3, 0.5, 200)])) is None


def test_walking_around_the_end_of_the_line_is_not_crossing_it() -> None:
    # The line runs from y=0.1 to y=0.9. Going around below it never crosses the segment.
    path: Path = [(0.4, 0.95, 0), (0.6, 0.95, 100), (0.7, 0.95, 200)]
    assert fired(module(), scene(path)) is None


def test_a_track_that_has_not_been_seen_enough_is_not_reported() -> None:
    assert fired(module(), scene(LEFT_TO_RIGHT, hits=1)) is None


def test_other_classes_are_ignored_by_default_and_included_on_request() -> None:
    car = scene(LEFT_TO_RIGHT, object_class=ObjectClass.VEHICLE_CAR)
    assert fired(module(), car) is None
    assert fired(module(object_classes=["person", "vehicle.car"]), car) is not None


def test_a_line_outside_its_schedule_is_silent() -> None:
    overnight = Schedule(start_minute=22 * 60, end_minute=5 * 60)  # the scene is at 00:00:00 UTC
    always_off = Schedule(start_minute=12 * 60, end_minute=13 * 60)
    m = module()
    assert fired(m, scene(LEFT_TO_RIGHT, lines=(line(schedule=overnight),))) is not None
    assert fired(m, scene(LEFT_TO_RIGHT, lines=(line(schedule=always_off),))) is None


def test_a_crossing_seen_only_between_guesses_is_not_reported_unless_allowed() -> None:
    path: Path = [(0.4, 0.5, 0), (0.45, 0.5, 100), (0.55, 0.5, 200), (0.6, 0.5, 300)]
    guessed = frozenset({2})  # the point just past the line is interpolated
    assert fired(module(), scene(path, interpolated=guessed)) is None
    assert fired(module(allow_interpolated=True), scene(path, interpolated=guessed)) is not None


def test_a_scene_without_lines_is_silence() -> None:
    assert module().observe(scene(LEFT_TO_RIGHT, lines=())) == ()


# ------------------------------------------------------------------ parameters and lifecycle


def test_unknown_parameters_are_an_error() -> None:
    with pytest.raises(ValidationError):
        LineCrossingParams.model_validate({"lookback": 3})


def test_parameters_that_could_never_fire_are_refused() -> None:
    with pytest.raises(ValidationError, match="object_classes"):
        LineCrossingParams(object_classes=())
    with pytest.raises(ValidationError, match=r"below 1.0"):
        LineCrossingParams(base_signal=0.95, velocity_bonus_ratio=0.1)


def test_observing_before_binding_is_a_programming_error() -> None:
    with pytest.raises(RuntimeError, match="before bind"):
        create().observe(scene(LEFT_TO_RIGHT))


def test_release_is_harmless() -> None:
    m = module()
    m.release()
    assert m.descriptor is MODULE
