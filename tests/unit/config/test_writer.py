"""The writer produces only files the loader accepts, writes atomically, and keeps secrets out."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from vigil.config import writer
from vigil.config.loader import load_settings
from vigil.config.schema.zones import ZoneSetConfig
from vigil.core.errors import ConfigError

GATE = {
    "camera_id": "gate-north",
    "name": "North gate",
    "source": {"kind": "rtsp", "url": "rtsp://cam.local:554/live", "credential_ref": "gate"},
    "priority": 7,
    "target_inference_fps": 8,
}
YARD = {
    "zones": [
        {
            "zone_id": "yard",
            "kind": "restricted",
            "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
            "criticality": 3,
        }
    ]
}


@pytest.fixture
def config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "vigil.yaml").write_text(
        "version: 1\ncameras:\n  - $include: cameras/*.yaml\n", encoding="utf-8"
    )
    return cfg


def test_a_written_camera_is_one_the_loader_accepts_and_comes_back_equal(config: Path) -> None:
    camera = writer.validate_camera(GATE)
    writer.write_camera(config, camera)
    assert writer.read_camera(config, "gate-north") == camera
    loaded = load_settings(config_dir=config).settings
    assert [c.camera_id for c in loaded.cameras] == ["gate-north"]
    assert loaded.cameras[0].source.credential_ref == "gate"


def test_the_file_is_small_and_holds_no_default_noise_and_no_secret(config: Path) -> None:
    path = writer.write_camera(config, writer.validate_camera(GATE))
    text = path.read_text(encoding="utf-8")
    assert "enabled" not in text and "trust_ratio" not in text  # defaults are left out
    assert "credential_ref: gate" in text  # the name, which is all the file may carry
    assert text.startswith("# Written by the VIGIL-88 console")


def test_a_camera_with_an_inline_password_is_refused_before_it_reaches_the_disk(
    config: Path,
) -> None:
    bad = {**GATE, "source": {"kind": "rtsp", "url": "rtsp://admin:hunter2@cam.local/live"}}
    with pytest.raises(ConfigError) as info:
        writer.validate_camera(bad)
    assert "must not contain credentials" in "".join(info.value.issues)
    assert writer.list_camera_files(config) == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"camera_id": "UPPER"},
        {"priority": 99},
        {"target_inference_fps": 0},
        {"source": {"kind": "webcam"}},
        {"source": {"kind": "bogus"}},
        {"zones_ref": "../../etc/passwd"},
    ],
)
def test_invalid_cameras_are_refused_with_the_field_named(mutation: dict[str, object]) -> None:
    with pytest.raises(ConfigError) as info:
        writer.validate_camera({**GATE, **mutation})
    assert info.value.issues


def test_the_camera_id_cannot_name_another_folder(config: Path) -> None:
    for bad in ("../x", "a/b", "..", "x" * 80, ""):
        with pytest.raises(ValueError):
            writer.camera_file_path(config, bad)


def test_a_write_that_fails_leaves_the_old_file_and_no_temp_file(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = writer.validate_camera(GATE)
    path = writer.write_camera(config, camera)
    before = path.read_text(encoding="utf-8")

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        writer.write_camera(config, writer.validate_camera({**GATE, "name": "Changed"}))
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == before  # the old file is intact
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # and nothing was left behind


def test_list_read_and_delete(config: Path) -> None:
    assert writer.list_camera_files(config) == [] and writer.read_camera(config, "nope") is None
    writer.write_camera(config, writer.validate_camera(GATE))
    writer.write_camera(config, writer.validate_camera({**GATE, "camera_id": "dock"}))
    assert writer.list_camera_files(config) == ["dock", "gate-north"]
    assert (
        writer.delete_camera(config, "dock") is True
        and writer.delete_camera(config, "dock") is False
    )
    assert writer.list_camera_files(config) == ["gate-north"]


def test_an_unreadable_camera_file_is_a_config_error_not_a_crash(config: Path) -> None:
    path = writer.camera_file_path(config, "bad")
    path.parent.mkdir(parents=True)
    path.write_text("camera_id: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError):
        writer.read_camera(config, "bad")
    path.write_text("x" * (writer.MAX_CAMERA_FILE_BYTES + 1), encoding="utf-8")
    with pytest.raises(ConfigError, match="too large"):
        writer.read_camera(config, "bad")


def test_zones_round_trip_through_the_same_validator_the_engine_uses(config: Path) -> None:
    zones = writer.validate_zone_set(YARD)
    path = writer.write_zone_set(config, "gate-north", zones)
    assert path.name == "gate-north.zones.yaml"
    assert ZoneSetConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))) == zones
    assert writer.zone_refs(config) == ["gate-north"]
    assert writer.delete_zone_set(config, "gate-north") is True
    assert writer.delete_zone_set(config, "gate-north") is False


@pytest.mark.parametrize(
    "zone",
    [
        {"zone_id": "bow", "kind": "restricted", "polygon": [[0, 0], [1, 1], [1, 0], [0, 1]]},
        {"zone_id": "out", "kind": "restricted", "polygon": [[0, 0], [2, 0], [2, 2]]},
        {"zone_id": "two", "kind": "restricted", "polygon": [[0, 0], [1, 1]]},
        {"zone_id": "line", "kind": "crossing_line", "polygon": [[0, 0], [1, 0], [1, 1]]},
        {"zone_id": "Bad ID", "kind": "restricted", "polygon": [[0, 0], [1, 0], [1, 1]]},
    ],
)
def test_a_zone_the_engine_would_refuse_is_refused_here(zone: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        writer.validate_zone_set({"zones": [zone]})


def test_a_zone_ref_cannot_be_a_path(config: Path) -> None:
    with pytest.raises(ConfigError):
        writer.write_zone_set(config, "../evil", writer.validate_zone_set(YARD))
