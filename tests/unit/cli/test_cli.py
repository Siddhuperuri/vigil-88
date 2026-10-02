from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import REPO_ROOT
from vigil.cli.config_doc import render_config_reference
from vigil.cli.main import app
from vigil.cli.sources import SourceSpecError, parse_source, parse_sources
from vigil.version import __version__

runner = CliRunner()


# ------------------------------------------------------------------ --source grammar


def test_webcam_source() -> None:
    cam = parse_source("webcam:3", 0)
    assert cam["camera_id"] == "cli-webcam-3" and cam["source"] == {
        "kind": "webcam",
        "device_index": 3,
    }


def test_synthetic_source_with_and_without_a_frame_count() -> None:
    assert parse_source("synthetic", 0)["source"] == {"kind": "synthetic"}
    assert parse_source("synthetic:25", 1)["source"] == {"kind": "synthetic", "frame_count": 25}
    assert parse_source("synthetic", 7)["camera_id"] == "cli-synthetic-7"


def test_rtsp_source_is_accepted_by_the_grammar() -> None:
    cam = parse_source("rtsp://10.0.0.5/s", 0)
    assert cam["source"] == {"kind": "rtsp", "url": "rtsp://10.0.0.5/s"}


@pytest.mark.parametrize(
    "bad", ["", "webcam", "webcam:x", "webcam:-1", "usb:0", "file.mp4", "synthetic:x"]
)
def test_bad_sources_are_rejected_with_the_valid_forms(bad: str) -> None:
    with pytest.raises(SourceSpecError, match="webcam:<index>"):
        parse_source(bad, 0)


def test_several_sources_get_distinct_ids() -> None:
    ids = [c["camera_id"] for c in parse_sources(["synthetic", "synthetic", "webcam:0"])]
    assert len(set(ids)) == 3


# ------------------------------------------------------------------ commands


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0 and result.stdout.strip() == __version__


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "run" in result.output and "doctor" in result.output


def test_selftest_passes() -> None:
    result = runner.invoke(app, ["run", "--selftest", "--config-dir", str(REPO_ROOT / "config")])
    assert result.exit_code == 0, result.output
    assert "selftest PASSED" in result.output and "FAIL" not in result.output.replace("PASSED", "")


def test_run_with_no_enabled_cameras_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "--config-dir", str(tmp_path / "config")])
    assert result.exit_code == 2 and "no enabled cameras" in result.output


def test_the_committed_example_camera_is_not_opened_implicitly() -> None:
    result = runner.invoke(app, ["run", "--config-dir", str(REPO_ROOT / "config")])
    assert result.exit_code == 2 and "no enabled cameras" in result.output


def test_bad_source_argument_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "--source", "bogus", "--config-dir", str(tmp_path / "c")])
    assert result.exit_code == 2 and "unrecognised source" in result.output


def test_invalid_configuration_is_reported_and_exits_2(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "vigil.yaml").write_text("vision: {max_batch_size: 0}\n", encoding="utf-8")
    result = runner.invoke(app, ["run", "--source", "synthetic", "--config-dir", str(cfg)])
    assert result.exit_code == 2 and "vision.max_batch_size" in result.output


def test_run_a_synthetic_camera_for_a_moment_then_shut_down_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--source",
            "synthetic:30",
            "--duration",
            "1",
            "--status-interval",
            "0.5",
            "--config-dir",
            str(tmp_path / "config"),
            "--log-level",
            "WARNING",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "state=running" in result.output and "inferred=30" in result.output
    assert "shutdown clean" in result.output


def test_run_with_only_an_rtsp_source_reports_not_implemented_and_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--source",
            "rtsp://10.0.0.5/s",
            "--duration",
            "0.3",
            "--status-interval",
            "0.2",
            "--config-dir",
            str(tmp_path / "config"),
            "--log-level",
            "ERROR",
        ],
    )
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_unimplemented_backend_fails_startup_with_a_clear_message(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "vigil.yaml").write_text("vision: {backend: ultralytics}\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run",
            "--source",
            "synthetic",
            "--duration",
            "0.2",
            "--config-dir",
            str(cfg),
            "--log-level",
            "ERROR",
        ],
    )
    assert (
        result.exit_code == 1
        and "startup failed" in result.output
        and "ultralytics" in result.output
    )


def test_doctor_reports_environment_config_and_capabilities() -> None:
    result = runner.invoke(app, ["doctor", "--config-dir", str(REPO_ROOT / "config")])
    assert result.exit_code == 0, result.output
    out = result.output
    for expected in (
        "Python",
        "capture extra",
        "torch",
        "vision.backend",
        "capabilities",
        "detection",
        "tracking",
    ):
        assert expected in out
    assert "none registered" in out  # no modules exist: say so rather than imply otherwise


def test_doctor_fails_on_broken_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "vigil.yaml").write_text("nonsense_key: 1\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--config-dir", str(cfg)])
    assert result.exit_code == 1 and "nonsense_key" in result.output


def test_doctor_flags_an_unimplemented_backend(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "vigil.yaml").write_text("vision: {backend: mock}\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--config-dir", str(cfg)])
    assert result.exit_code == 1 and "not implemented" in result.output


def test_doctor_dump_config_writes_the_reference(tmp_path: Path) -> None:
    target = tmp_path / "out" / "CONFIG.md"
    result = runner.invoke(
        app, ["doctor", "--config-dir", str(REPO_ROOT / "config"), "--dump-config", str(target)]
    )
    assert result.exit_code == 0 and target.read_text(encoding="utf-8").startswith(
        "# Configuration"
    )


# ------------------------------------------------------------------ generated reference


def test_config_reference_covers_every_section_and_nested_keys() -> None:
    doc = render_config_reference()
    for key in (
        "`temporal.l_activate`",
        "`vision.max_batch_size`",
        "`api.auth.token`",
        "`cameras[].source.kind`",
        "`severity.weights.base`",
        "`ingest.reconnect_base_ms`",
    ):
        assert key in doc
    assert "ge=1" in doc  # constraints are documented


def test_the_committed_config_reference_is_up_to_date() -> None:
    committed = (REPO_ROOT / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    assert committed == render_config_reference(), (
        "run: uv run vigil doctor --dump-config docs/CONFIG.md"
    )
