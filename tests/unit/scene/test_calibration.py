"""The ground plane: the homography, its honesty about its own limits, and the loader.

The property worth most here is that a conversion is **refused** rather than approximated
whenever it cannot be made — off a calibrated frame size, at the horizon, with no velocity to
convert. A plausible-looking number in metres that was really pixels would be invisible
everywhere downstream, which is why `velocity_m_s` is never derived from `velocity_px_s`.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from tests.unit.scene.test_scene import meta as scene_meta
from tests.unit.scene.test_scene import track as scene_track
from vigil.config.calibration import (
    CALIBRATION_SUFFIX,
    MAX_CALIBRATION_FILE_BYTES,
    calibration_file_path,
    load_calibration,
)
from vigil.config.schema.calibration import CalibrationConfig
from vigil.config.schema.modules import SceneConfig
from vigil.core.errors import ConfigError
from vigil.core.geometry import Point, Vector2
from vigil.domain.enums import SceneFact
from vigil.domain.track import TrackedObject
from vigil.scene.analyzer import SceneAnalyzer
from vigil.scene.calibration import (
    REQUIRED_POINTS,
    CalibrationError,
    Correspondence,
    GroundPlane,
    ground_plane_from_config,
)

WIDTH = 1280
HEIGHT = 720

# A pure scale: 100 px is 1 m everywhere, no perspective. Every answer is checkable by hand.
SCALED = [
    Correspondence(Point(0.0, 0.0), Point(0.0, 0.0)),
    Correspondence(Point(100.0, 0.0), Point(1.0, 0.0)),
    Correspondence(Point(100.0, 100.0), Point(1.0, 1.0)),
    Correspondence(Point(0.0, 100.0), Point(0.0, 1.0)),
]

# A trapezoid on screen that is a 2 m x 3 m rectangle on the floor: a real oblique view.
OBLIQUE = [
    Correspondence(Point(400.0, 700.0), Point(0.0, 0.0)),
    Correspondence(Point(880.0, 700.0), Point(2.0, 0.0)),
    Correspondence(Point(760.0, 400.0), Point(2.0, 3.0)),
    Correspondence(Point(520.0, 400.0), Point(0.0, 3.0)),
]


def _plane(correspondences: list[Correspondence] = SCALED) -> GroundPlane:
    return GroundPlane.fit(
        camera_id="cam",
        width_px=WIDTH,
        height_px=HEIGHT,
        correspondences=correspondences,
    )


# ---------------------------------------------------------------- the fit


def test_a_pure_scale_maps_pixels_to_metres_exactly() -> None:
    plane = _plane()
    assert plane.reprojection_error_px == pytest.approx(0.0, abs=1e-9)
    world = plane.to_world_m(Point(250.0, 400.0))
    assert world is not None
    assert (world.x, world.y) == pytest.approx((2.5, 4.0))


def test_an_oblique_view_reproduces_its_own_points() -> None:
    """An exact four-point fit must pass through all four, or the solve is wrong."""
    plane = _plane(OBLIQUE)
    assert plane.reprojection_error_px == pytest.approx(0.0, abs=1e-6)
    for c in OBLIQUE:
        world = plane.to_world_m(c.pixel)
        assert world is not None
        assert (world.x, world.y) == pytest.approx((c.world_m.x, c.world_m.y), abs=1e-9)


def test_the_jacobian_matches_a_finite_difference() -> None:
    """The analytic derivative is the whole basis of the velocity conversion, so check it
    against a numerical one. They must agree to several digits on a genuinely curved map."""
    plane = _plane(OBLIQUE)
    probe = Point(640.0, 560.0)
    jacobian = plane.jacobian_at(probe)
    assert jacobian is not None
    step = 1e-4
    here = plane.to_world_m(probe)
    right = plane.to_world_m(Point(probe.x + step, probe.y))
    down = plane.to_world_m(Point(probe.x, probe.y + step))
    assert here is not None and right is not None and down is not None
    assert jacobian[0] == pytest.approx((right.x - here.x) / step, rel=1e-4)
    assert jacobian[2] == pytest.approx((right.y - here.y) / step, rel=1e-4)
    # dX/dy is ~0 at the frame's horizontal centre of a symmetric trapezoid, so this pair
    # needs an absolute tolerance: two near-zero numbers have no meaningful relative error.
    assert jacobian[1] == pytest.approx((down.x - here.x) / step, rel=1e-3, abs=1e-6)
    assert jacobian[3] == pytest.approx((down.y - here.y) / step, rel=1e-3, abs=1e-6)


def test_a_velocity_converts_at_the_scale_of_its_own_position() -> None:
    plane = _plane()
    converted = plane.to_world_velocity(Point(50.0, 50.0), Vector2(100.0, -200.0))
    assert converted is not None
    assert (converted.x, converted.y) == pytest.approx((1.0, -2.0))


def test_perspective_makes_the_same_pixel_speed_a_different_ground_speed() -> None:
    """The reason a single metres-per-pixel figure was refused: on this camera the far end of
    the calibrated patch is more than twice the scale of the near end."""
    plane = _plane(OBLIQUE)
    near = plane.to_world_velocity(Point(640.0, 700.0), Vector2(100.0, 0.0))
    far = plane.to_world_velocity(Point(640.0, 400.0), Vector2(100.0, 0.0))
    assert near is not None and far is not None
    assert far.magnitude > near.magnitude * 1.5
    spread = plane.scale_spread()
    assert spread is not None and spread > 1.5


def test_the_calibrated_extent_is_reported_for_context() -> None:
    assert _plane(OBLIQUE).extent_m() == pytest.approx((2.0, 3.0))


def test_metres_per_pixel_is_the_square_root_of_the_area_scale() -> None:
    plane = _plane()
    assert plane.metres_per_pixel_at(Point(10.0, 10.0)) == pytest.approx(0.01)


# ---------------------------------------------------------------- refusals


def test_three_collinear_points_are_refused_rather_than_fitted() -> None:
    collinear = [
        Correspondence(Point(0.0, 0.0), Point(0.0, 0.0)),
        Correspondence(Point(100.0, 0.0), Point(1.0, 0.0)),
        Correspondence(Point(200.0, 0.0), Point(2.0, 0.0)),
        Correspondence(Point(0.0, 100.0), Point(0.0, 1.0)),
    ]
    with pytest.raises(CalibrationError, match="collinear"):
        _plane(collinear)


def test_the_wrong_number_of_points_is_refused() -> None:
    with pytest.raises(CalibrationError, match=f"exactly {REQUIRED_POINTS}"):
        _plane(SCALED[:3])


def test_a_calibration_belongs_to_one_frame_size() -> None:
    """A homography is in pixels. Scaling one to another resolution by an unchecked factor is
    exactly the kind of silent wrongness this project refuses."""
    plane = _plane()
    assert plane.matches(WIDTH, HEIGHT)
    assert not plane.matches(640, 360)


def test_a_point_on_the_horizon_has_no_ground_position() -> None:
    """Constructed directly: a homography whose third row sends this pixel to w = 0."""
    plane = GroundPlane(
        camera_id="cam",
        width_px=WIDTH,
        height_px=HEIGHT,
        correspondences=tuple(SCALED),
        matrix=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, -10.0),
        reprojection_error_px=0.0,
    )
    horizon = Point(10.0, 0.0)
    assert plane.to_world_m(horizon) is None
    assert plane.jacobian_at(horizon) is None
    assert plane.metres_per_pixel_at(horizon) is None
    assert plane.to_world_velocity(horizon, Vector2(1.0, 1.0)) is None


def test_reprojection_error_is_reported_and_is_zero_by_construction() -> None:
    """Why it is not a tolerance.

    A four-point fit passes exactly through its four points, even for a quad so thin that the
    solve is the thing at risk: measured here at a 1e-3 px tilt over a 1000 px baseline. A
    threshold on this number could not fire, so `fit` reports it and compares it to nothing.
    """
    nearly_collinear = [
        Correspondence(Point(0.0, 0.0), Point(0.0, 0.0)),
        Correspondence(Point(1000.0, 0.0), Point(10.0, 0.0)),
        Correspondence(Point(1000.0, 0.001), Point(10.0, 5.0)),
        Correspondence(Point(0.0, 0.002), Point(0.0, 5.0)),
    ]
    plane = _plane(nearly_collinear)
    assert plane.reprojection_error_px < 1e-6


def test_the_measurement_itself_reports_a_matrix_that_misses_its_points() -> None:
    """The measurement has real logic -- a world-space residual pushed back into pixels through
    the inverse Jacobian -- so it is checked against a matrix that did not come from `fit`."""
    off_by_ten_px = GroundPlane(
        camera_id="cam",
        width_px=WIDTH,
        height_px=HEIGHT,
        correspondences=tuple(SCALED),
        # The same 100 px/m scale, shifted 0.1 m: every point lands 10 px from where it should.
        matrix=(0.01, 0.0, 0.1, 0.0, 0.01, 0.0, 0.0, 0.0, 1.0),
        reprojection_error_px=0.0,
    )
    assert off_by_ten_px.measure_reprojection_error() == pytest.approx(10.0)


def test_the_measurement_is_infinite_where_no_point_can_be_mapped() -> None:
    beyond_the_horizon = GroundPlane(
        camera_id="cam",
        width_px=WIDTH,
        height_px=HEIGHT,
        correspondences=tuple(SCALED),
        matrix=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
        reprojection_error_px=0.0,
    )
    assert math.isinf(beyond_the_horizon.measure_reprojection_error())


# ---------------------------------------------------------------- the config schema


def _config(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "version": 1,
        "width_px": WIDTH,
        "height_px": HEIGHT,
        "points": [
            {"pixel": [c.pixel.x, c.pixel.y], "world_m": [c.world_m.x, c.world_m.y]}
            for c in OBLIQUE
        ],
    }
    body.update(overrides)
    return body


def test_a_valid_file_becomes_a_usable_plane() -> None:
    config = CalibrationConfig.model_validate(_config(description="lobby"))
    plane = ground_plane_from_config(config, "cam")
    assert plane.camera_id == "cam"
    assert plane.description == "lobby"
    assert plane.reprojection_error_px < 1e-6


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"points": []}, "exactly 4 points"),
        ({"points": [{"pixel": [0, 0], "world_m": [0, 0]}]}, "exactly 4 points"),
        ({"width_px": 8}, "greater than or equal to 16"),
    ],
)
def test_a_structurally_wrong_file_is_refused(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CalibrationConfig.model_validate(_config(**overrides))


def test_a_pixel_outside_the_stated_frame_is_refused() -> None:
    points = [
        {"pixel": [c.pixel.x, c.pixel.y], "world_m": [c.world_m.x, c.world_m.y]} for c in OBLIQUE
    ]
    points[0]["pixel"] = [WIDTH + 1.0, 10.0]
    with pytest.raises(ValueError, match="outside the stated frame"):
        CalibrationConfig.model_validate(_config(points=points))


def test_a_repeated_click_is_refused() -> None:
    points = [
        {"pixel": [c.pixel.x, c.pixel.y], "world_m": [c.world_m.x, c.world_m.y]} for c in OBLIQUE
    ]
    points[1]["pixel"] = points[0]["pixel"]
    with pytest.raises(ValueError, match="same click"):
        CalibrationConfig.model_validate(_config(points=points))


def test_two_measurements_of_the_same_floor_spot_are_refused() -> None:
    points = [
        {"pixel": [c.pixel.x, c.pixel.y], "world_m": [c.world_m.x, c.world_m.y]} for c in OBLIQUE
    ]
    points[1]["world_m"] = points[0]["world_m"]
    with pytest.raises(ValueError, match="same spot"):
        CalibrationConfig.model_validate(_config(points=points))


def test_points_measured_along_a_line_are_refused_in_world_space() -> None:
    """Collinear *world* points mean the operator measured along an edge; collinear *pixels*
    mean they clicked along one. Both are caught, and the message says which."""
    points = [
        {"pixel": [c.pixel.x, c.pixel.y], "world_m": [c.world_m.x, c.world_m.y]} for c in OBLIQUE
    ]
    points[0]["world_m"] = [0.0, 1.0]
    points[1]["world_m"] = [0.0, 2.0]
    points[2]["world_m"] = [0.0, 3.0]
    points[3]["world_m"] = [0.0, 4.0]
    with pytest.raises(ValueError, match="world_m points"):
        CalibrationConfig.model_validate(_config(points=points))


def test_a_world_coordinate_far_from_the_origin_is_refused() -> None:
    points = [
        {"pixel": [c.pixel.x, c.pixel.y], "world_m": [c.world_m.x, c.world_m.y]} for c in OBLIQUE
    ]
    points[0]["world_m"] = [5000.0, 0.0]
    with pytest.raises(ValueError, match="of the origin"):
        CalibrationConfig.model_validate(_config(points=points))


# ---------------------------------------------------------------- the loader


def _write(config_dir: Path, ref: str, body: object) -> None:
    folder = config_dir / "calibration"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{ref}{CALIBRATION_SUFFIX}").write_text(yaml.safe_dump(body), encoding="utf-8")


def test_a_ref_selects_a_file_inside_the_config_directory(tmp_path: Path) -> None:
    assert calibration_file_path(tmp_path, "lobby-floor") == (
        tmp_path / "calibration" / f"lobby-floor{CALIBRATION_SUFFIX}"
    )


@pytest.mark.parametrize(
    "ref", ["../escape", "../../etc/passwd", "sub/dir", "C:/evil", "with space", "UPPER", ""]
)
def test_a_ref_that_is_not_a_slug_cannot_reach_out_of_the_config_directory(
    tmp_path: Path, ref: str
) -> None:
    with pytest.raises(ConfigError, match="invalid calibration_ref"):
        calibration_file_path(tmp_path, ref)


def test_a_missing_file_names_the_ref(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="calibration file not found") as caught:
        load_calibration(tmp_path, "lobby")
    assert tuple(caught.value.issues) == ("calibration_ref: lobby",)


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "lobby", ["not", "a", "mapping"])
    with pytest.raises(ConfigError, match="must contain a mapping"):
        load_calibration(tmp_path, "lobby")


def test_an_empty_file_is_refused_for_what_it_is_missing(tmp_path: Path) -> None:
    (tmp_path / "calibration").mkdir()
    (tmp_path / "calibration" / f"lobby{CALIBRATION_SUFFIX}").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid calibration file") as caught:
        load_calibration(tmp_path, "lobby")
    assert any("width_px" in issue for issue in caught.value.issues)


def test_unreadable_yaml_is_reported_not_raised_raw(tmp_path: Path) -> None:
    (tmp_path / "calibration").mkdir()
    (tmp_path / "calibration" / f"lobby{CALIBRATION_SUFFIX}").write_text(
        "points: [unclosed", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="cannot read calibration file"):
        load_calibration(tmp_path, "lobby")


def test_an_oversized_file_is_refused_before_it_is_parsed(tmp_path: Path) -> None:
    (tmp_path / "calibration").mkdir()
    (tmp_path / "calibration" / f"lobby{CALIBRATION_SUFFIX}").write_text(
        "# " + "x" * (MAX_CALIBRATION_FILE_BYTES + 1), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="larger than"):
        load_calibration(tmp_path, "lobby")


def test_a_round_trip_through_the_loader_produces_the_same_plane(tmp_path: Path) -> None:
    _write(tmp_path, "lobby", _config())
    plane = ground_plane_from_config(load_calibration(tmp_path, "lobby"), "cam")
    direct = _plane(OBLIQUE)
    assert plane.matrix == pytest.approx(direct.matrix)


def test_the_committed_example_calibration_is_valid() -> None:
    """The example file ships in `config/`; an invalid one would be an unusable template."""
    plane = ground_plane_from_config(
        load_calibration(Path("config"), "example-floor"), "example-webcam"
    )
    assert plane.reprojection_error_px < 1e-6
    extent = plane.extent_m()
    assert extent is not None and extent[0] > 0 and extent[1] > 0
    spread = plane.scale_spread()
    assert spread is not None and math.isfinite(spread)


# ---------------------------------------------------------------- through the analyzer

_SCENE_WIDTH, _SCENE_HEIGHT = 640, 480

# 100 px is 1 m, over the scene fixture's frame rather than this module's.
_SCENE_PLANE = GroundPlane.fit(
    camera_id="test-cam",
    width_px=_SCENE_WIDTH,
    height_px=_SCENE_HEIGHT,
    correspondences=[
        Correspondence(Point(0.0, 0.0), Point(0.0, 0.0)),
        Correspondence(Point(100.0, 0.0), Point(1.0, 0.0)),
        Correspondence(Point(100.0, 100.0), Point(1.0, 1.0)),
        Correspondence(Point(0.0, 100.0), Point(0.0, 1.0)),
    ],
)


def _analyze(plane: GroundPlane | None, **track_kwargs: object) -> TrackedObject:
    analyzer = SceneAnalyzer(camera_id="test-cam", config=SceneConfig(), ground_plane=plane)
    state = analyzer.analyze([scene_track(1, **track_kwargs)], scene_meta())  # type: ignore[arg-type]
    return state.tracks[0]


def test_an_uncalibrated_camera_reports_no_metric_velocity() -> None:
    """The field stays None. It is never a px/s number relabelled as metres."""
    reported = _analyze(None, foot=(300.0, 400.0), velocity=(100.0, 0.0))
    assert reported.velocity_px_s is not None
    assert reported.velocity_m_s is None
    assert reported.speed_m_s is None
    assert reported.speed_px_s == pytest.approx(100.0)


def test_a_calibrated_camera_reports_both_units() -> None:
    reported = _analyze(_SCENE_PLANE, foot=(300.0, 400.0), velocity=(100.0, -50.0))
    assert reported.speed_px_s == pytest.approx(math.hypot(100.0, 50.0))
    assert reported.velocity_m_s is not None
    assert (reported.velocity_m_s.x, reported.velocity_m_s.y) == pytest.approx((1.0, -0.5))
    assert reported.speed_m_s == pytest.approx(math.hypot(1.0, 0.5))


def test_the_conversion_happens_at_the_foot_point_not_the_box_centre() -> None:
    """The feet are on the calibrated floor; the chest is a metre above it. On a perspective
    calibration the two give different answers, and only one of them is a floor speed."""
    oblique = GroundPlane.fit(
        camera_id="test-cam",
        width_px=_SCENE_WIDTH,
        height_px=_SCENE_HEIGHT,
        correspondences=[
            Correspondence(Point(200.0, 460.0), Point(0.0, 0.0)),
            Correspondence(Point(440.0, 460.0), Point(2.0, 0.0)),
            Correspondence(Point(380.0, 260.0), Point(2.0, 3.0)),
            Correspondence(Point(260.0, 260.0), Point(0.0, 3.0)),
        ],
    )
    reported = _analyze(oblique, foot=(320.0, 440.0), velocity=(100.0, 0.0))
    assert reported.velocity_m_s is not None
    at_foot = oblique.to_world_velocity(reported.bbox.bottom_center, reported.velocity_px_s)
    at_centre = oblique.to_world_velocity(reported.bbox.center, reported.velocity_px_s)
    assert at_foot is not None and at_centre is not None
    assert at_foot.magnitude != pytest.approx(at_centre.magnitude)
    assert reported.velocity_m_s.magnitude == pytest.approx(at_foot.magnitude)


def test_a_track_with_no_velocity_gets_no_metric_velocity() -> None:
    reported = _analyze(_SCENE_PLANE, foot=(300.0, 400.0))
    assert reported.velocity_px_s is None
    assert reported.velocity_m_s is None


def test_a_calibration_for_another_frame_size_degrades_rather_than_converting() -> None:
    """A homography is in pixels. Applying one taken at 1280x720 to a 640x480 frame would
    produce confident nonsense, so the fact is declared missing instead."""
    wrong_size = GroundPlane.fit(
        camera_id="test-cam",
        width_px=1280,
        height_px=720,
        correspondences=list(SCALED),
    )
    analyzer = SceneAnalyzer(camera_id="test-cam", config=SceneConfig(), ground_plane=wrong_size)
    state = analyzer.analyze(
        [scene_track(1, foot=(300.0, 400.0), velocity=(100.0, 0.0))], scene_meta()
    )
    assert SceneFact.GROUND_PLANE in state.degraded_facts
    assert state.tracks[0].velocity_m_s is None


def test_an_uncalibrated_camera_does_not_report_the_ground_plane_as_degraded() -> None:
    """Absent on purpose is not a degradation: nothing asked for a ground plane here."""
    analyzer = SceneAnalyzer(camera_id="test-cam", config=SceneConfig())
    state = analyzer.analyze(
        [scene_track(1, foot=(300.0, 400.0), velocity=(100.0, 0.0))], scene_meta()
    )
    assert SceneFact.GROUND_PLANE not in state.degraded_facts
    assert analyzer.ground_plane is None
