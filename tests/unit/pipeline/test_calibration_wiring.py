"""Does a calibration file actually reach the capability ledger and the analyzer?

The unit tests for the homography prove the arithmetic. What is checked here is the part that
decides what an operator is told: a camera with a usable calibration gets `GROUND_PLANE`, one
with a broken calibration gets a **named reason** and keeps working in px/s, and one that never
asked for a calibration is not reported as degraded for lacking it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from vigil.config.loader import LoadedConfig
from vigil.config.schema.camera import CameraConfig
from vigil.core.capabilities import HOW_TO_PROVIDE, Capability
from vigil.core.clock import ManualClock
from vigil.pipeline.app import Application
from vigil.pipeline.assembly import load_camera_ground_plane

MakeConfig = Callable[..., LoadedConfig]

CAMERA = "lobby"
REF = "lobby-floor"

GOOD = """
version: 1
width_px: 320
height_px: 240
description: four corners of the mat
points:
  - pixel: [100.0, 220.0]
    world_m: [0.0, 0.0]
  - pixel: [220.0, 220.0]
    world_m: [2.0, 0.0]
  - pixel: [190.0, 140.0]
    world_m: [2.0, 3.0]
  - pixel: [130.0, 140.0]
    world_m: [0.0, 3.0]
"""

# Three points along the bottom edge: the solve cannot determine a plane from these.
COLLINEAR = """
version: 1
width_px: 320
height_px: 240
points:
  - pixel: [100.0, 220.0]
    world_m: [0.0, 0.0]
  - pixel: [150.0, 220.0]
    world_m: [1.0, 0.0]
  - pixel: [200.0, 220.0]
    world_m: [2.0, 0.0]
  - pixel: [150.0, 140.0]
    world_m: [1.0, 3.0]
"""


def _write(config_dir: Path, body: str, ref: str = REF) -> None:
    folder = config_dir / "calibration"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{ref}.calib.yaml").write_text(body, encoding="utf-8")


def _camera(**overrides: object) -> dict[str, object]:
    return {
        "camera_id": CAMERA,
        "source": {"kind": "synthetic", "width_px": 320, "height_px": 240, "fps": 5},
        "enabled": True,
        **overrides,
    }


# ---------------------------------------------------------------- the loader seam


def test_a_camera_that_asks_for_no_calibration_has_no_plane_and_no_issue(
    tmp_path: Path,
) -> None:
    result = load_camera_ground_plane(tmp_path, CameraConfig.model_validate(_camera()))
    assert result.plane is None
    assert result.issue is None


def test_a_good_file_becomes_a_plane(tmp_path: Path) -> None:
    _write(tmp_path, GOOD)
    result = load_camera_ground_plane(
        tmp_path, CameraConfig.model_validate(_camera(calibration_ref=REF))
    )
    assert result.issue is None
    assert result.plane is not None
    assert result.plane.camera_id == CAMERA
    assert result.plane.matches(320, 240)


def test_a_missing_file_is_an_issue_not_an_exception(tmp_path: Path) -> None:
    result = load_camera_ground_plane(
        tmp_path, CameraConfig.model_validate(_camera(calibration_ref="absent"))
    )
    assert result.plane is None
    assert result.issue is not None
    assert "not found" in result.issue
    assert "absent" in result.issue


def test_a_degenerate_file_is_an_issue_naming_the_problem(tmp_path: Path) -> None:
    _write(tmp_path, COLLINEAR)
    result = load_camera_ground_plane(
        tmp_path, CameraConfig.model_validate(_camera(calibration_ref=REF))
    )
    assert result.plane is None
    assert result.issue is not None
    assert "collinear" in result.issue


def test_a_calibration_ref_must_be_a_name_not_a_path() -> None:
    """Refused at config validation, before anything touches the filesystem."""
    with pytest.raises(ValueError, match="never a path"):
        CameraConfig.model_validate(_camera(calibration_ref="../../etc/passwd"))


def test_the_capability_says_what_would_provide_it() -> None:
    how = HOW_TO_PROVIDE[Capability.GROUND_PLANE]
    assert "calibration_ref" in how
    assert "planned" not in how


# ---------------------------------------------------------------- through the application


def _application(loaded: LoadedConfig) -> Application:
    return Application(loaded.settings, loaded.paths, clock=ManualClock())


def test_a_calibrated_camera_grants_the_capability(make_config: MakeConfig) -> None:
    loaded = make_config({"cameras": [_camera(calibration_ref=REF)]})
    _write(loaded.paths.config_dir, GOOD)
    app = _application(loaded)
    app.start()
    try:
        assert Capability.GROUND_PLANE in app.capabilities.available()
        status = app.capabilities.status(Capability.GROUND_PLANE)
        assert status.available is True
        assert status.provider is not None and REF in status.provider
    finally:
        app.stop()


def test_an_uncalibrated_camera_does_not_grant_it_and_says_how(
    make_config: MakeConfig,
) -> None:
    loaded = make_config({"cameras": [_camera()]})
    app = _application(loaded)
    app.start()
    try:
        assert Capability.GROUND_PLANE not in app.capabilities.available()
        status = app.capabilities.status(Capability.GROUND_PLANE)
        assert status.available is False
        assert status.how_to_provide is not None
        assert "calibration_ref" in status.how_to_provide
    finally:
        app.stop()


def test_a_broken_calibration_is_reported_and_the_camera_still_runs(
    make_config: MakeConfig,
) -> None:
    """Fail open, loudly: monitoring in pixels beats not monitoring (01 §6)."""
    loaded = make_config({"cameras": [_camera(calibration_ref=REF)]})
    _write(loaded.paths.config_dir, COLLINEAR)
    app = _application(loaded)
    app.start()
    try:
        assert Capability.GROUND_PLANE not in app.capabilities.available()
        assert app.analysis is not None
        issue = app.analysis.calibration_issues[CAMERA]
        assert "collinear" in issue
        # The camera is up: a broken calibration costs a unit, not a pipeline.
        assert Capability.VELOCITY in app.capabilities.available()
        assert app.analysis.capabilities_by_camera[CAMERA]
    finally:
        app.stop()
