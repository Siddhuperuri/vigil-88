"""Tripwires: the geometry, the zone-file format, and how a line reaches the scene."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tests.unit.scene.test_scene import CAM, meta
from vigil.config.loader import load_settings
from vigil.config.schema.camera import CameraConfig
from vigil.config.schema.modules import SceneConfig
from vigil.config.schema.zones import LineConfig, ZoneConfig, ZoneSetConfig
from vigil.core.capabilities import Capability
from vigil.core.geometry import Point, crossing_direction, side_of_line
from vigil.domain.enums import CrossingDirection
from vigil.domain.scene import CrossingLine
from vigil.pipeline.assembly import load_camera_zones
from vigil.scene.analyzer import SceneAnalyzer
from vigil.scene.zones import lines_from_set

A, B = Point(10.0, 0.0), Point(10.0, 20.0)  # a vertical segment

# ------------------------------------------------------------------ geometry


def test_side_is_signed_and_zero_on_the_line() -> None:
    assert side_of_line(A, B, Point(5.0, 10.0)) > 0
    assert side_of_line(A, B, Point(15.0, 10.0)) < 0
    assert side_of_line(A, B, Point(10.0, 5.0)) == 0.0


@pytest.mark.parametrize(
    ("p0", "p1", "expected"),
    [
        ((15.0, 10.0), (5.0, 10.0), 1),  # negative side to positive side
        ((5.0, 10.0), (15.0, 10.0), -1),
        ((5.0, 10.0), (8.0, 10.0), 0),  # never reached the line
        ((15.0, 25.0), (5.0, 25.0), 0),  # crossed the infinite line, beyond the segment's end
        ((15.0, -3.0), (5.0, -3.0), 0),
        ((10.0, 10.0), (5.0, 10.0), 0),  # starts exactly on the line: not a crossing by itself
        ((15.0, 10.0), (10.0, 10.0), 0),  # ends exactly on it
        ((15.0, 1.0), (5.0, 19.0), 1),  # a diagonal that stays within the segment
    ],
)
def test_crossing_direction(
    p0: tuple[float, float], p1: tuple[float, float], expected: int
) -> None:
    assert crossing_direction(A, B, Point(*p0), Point(*p1)) == expected


def test_a_move_that_touches_the_end_of_the_segment_counts() -> None:
    assert crossing_direction(A, B, Point(15.0, 0.0), Point(5.0, 0.0)) == 1


# ------------------------------------------------------------------ the zone file


def line(**change: object) -> LineConfig:
    base: dict[str, object] = {"line_id": "door", "start": (0.2, 0.2), "end": (0.8, 0.2)}
    return LineConfig.model_validate({**base, **change})


def test_a_line_needs_two_distinct_visible_points() -> None:
    assert line().direction == "any" and line().display_label == "door"
    with pytest.raises(ValidationError, match="shorter than 1%"):
        line(end=(0.2001, 0.2))
    with pytest.raises(ValidationError, match="normalized"):
        line(end=(1.5, 0.2))
    with pytest.raises(ValidationError, match="line_id"):
        line(line_id="Not A Slug")
    with pytest.raises(ValidationError, match="direction"):
        line(direction="sideways")


def test_ids_are_unique_across_areas_and_lines() -> None:
    square = (
        (0.1, 0.1),
        (0.3, 0.1),
        (0.3, 0.3),
        (0.1, 0.3),
    )
    zone = ZoneConfig(zone_id="door", kind="restricted", polygon=square)
    with pytest.raises(ValidationError, match="duplicate zone or line id"):
        ZoneSetConfig(zones=(zone,), lines=(line(),))


def test_the_old_file_format_without_lines_still_loads() -> None:
    assert ZoneSetConfig.model_validate({"version": 1, "zones": []}).lines == ()


def test_a_crossing_line_zone_kind_points_to_the_lines_list() -> None:
    with pytest.raises(ValidationError, match="under `lines:`"):
        ZoneConfig(zone_id="x", kind="crossing_line", polygon=((0, 0), (1, 0), (1, 1)))


def test_a_line_becomes_a_domain_crossing_line() -> None:
    cfg = ZoneSetConfig(lines=(line(direction="to_right", criticality=3, label="Back door"),))
    (made,) = lines_from_set(cfg, CAM, max_lines=8)
    assert made.direction is CrossingDirection.TO_RIGHT and made.criticality == 3
    assert made.label == "Back door" and made.start == Point(0.2, 0.2)


def test_the_cap_on_lines_applies() -> None:
    cfg = ZoneSetConfig(lines=tuple(line(line_id=f"l{i}") for i in range(5)))
    assert len(lines_from_set(cfg, CAM, max_lines=2)) == 2


def test_the_domain_line_refuses_a_point_or_a_wild_coordinate() -> None:
    kwargs = {
        "line_id": "x",
        "camera_id": CAM,
        "direction": CrossingDirection.ANY,
        "criticality": 0,
        "active_schedule": None,
        "label": "x",
    }
    with pytest.raises(ValueError, match="two different points"):
        CrossingLine(start=Point(0.5, 0.5), end=Point(0.5, 0.5), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="normalized"):
        CrossingLine(start=Point(0.5, 0.5), end=Point(2.0, 0.5), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="criticality"):
        CrossingLine(start=Point(0.1, 0.5), end=Point(0.9, 0.5), **{**kwargs, "criticality": 9})  # type: ignore[arg-type]


# ------------------------------------------------------------------ into the scene


def test_the_analyzer_hands_its_lines_to_the_scene_and_can_swap_them() -> None:
    cfg = ZoneSetConfig(lines=(line(),))
    lines = lines_from_set(cfg, CAM, max_lines=8)
    analyzer = SceneAnalyzer(camera_id=CAM, config=SceneConfig(), lines=lines)
    assert analyzer.analyze([], meta()).lines == lines
    analyzer.set_lines(())
    assert analyzer.analyze([], meta(1, 100.0)).lines == ()


def test_a_scene_built_without_lines_has_none() -> None:
    assert SceneAnalyzer(camera_id=CAM, config=SceneConfig()).analyze([], meta()).lines == ()


# ------------------------------------------------------------------ into the pipeline


def write_zone_file(config_dir: Path, body: dict[str, object]) -> None:
    folder = config_dir / "zones"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "gate.zones.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def camera() -> CameraConfig:
    return CameraConfig.model_validate(
        {"camera_id": CAM, "source": {"kind": "synthetic"}, "zones_ref": "gate"}
    )


def test_a_file_with_only_lines_gives_the_camera_its_zones_capability(tmp_path: Path) -> None:
    write_zone_file(
        tmp_path,
        {"version": 1, "lines": [{"line_id": "d", "start": [0.1, 0.5], "end": [0.9, 0.5]}]},
    )
    loaded = load_camera_zones(tmp_path, camera(), max_zones=8)
    assert loaded.issue is None and loaded.zones == () and len(loaded.lines) == 1 and loaded.any


def test_a_file_with_neither_is_reported_as_empty(tmp_path: Path) -> None:
    write_zone_file(tmp_path, {"version": 1})
    loaded = load_camera_zones(tmp_path, camera(), max_zones=8)
    assert not loaded.any and "no zones or lines" in (loaded.issue or "")


def test_the_settings_loader_is_unaffected_by_lines(tmp_path: Path) -> None:
    assert load_settings(config_dir=tmp_path, env={}, use_dotenv=False).settings.cameras == ()
    assert Capability.ZONES.value == "zones"


def test_a_swap_requested_from_another_thread_is_applied_on_the_next_frame() -> None:
    import threading

    analyzer = SceneAnalyzer(camera_id=CAM, config=SceneConfig())
    assert analyzer.analyze([], meta()).lines == ()
    lines = lines_from_set(ZoneSetConfig(lines=(line(),)), CAM, max_lines=8)
    worker = threading.Thread(target=analyzer.request_swap, args=((), lines))
    worker.start()
    worker.join()
    assert analyzer.lines_in_force == ()  # nothing changed under the analysis thread's feet
    assert analyzer.analyze([], meta(1, 100.0)).lines == lines
    assert analyzer.analyze([], meta(2, 200.0)).lines == lines  # applied once, then kept
