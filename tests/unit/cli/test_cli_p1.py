"""CLI behaviour for the P1 commands: models, bench, and run with a detector and --serve."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.support.onnx_models import PlantedBox, write_model
from vigil.cli.main import app

runner = CliRunner()


def config_dir(tmp_path: Path) -> str:
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    return str(cfg)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ------------------------------------------------------------------ models


def test_models_list_shows_the_registry_and_that_nothing_is_downloaded(tmp_path: Path) -> None:
    r = runner.invoke(app, ["models", "list", "--config-dir", config_dir(tmp_path)])
    assert r.exit_code == 0
    for name in ("yolox_nano", "yolox_tiny", "yolox_s"):
        assert name in r.output
    assert r.output.count("not downloaded") == 3 and "Apache-2.0" in r.output


def test_fetching_an_unknown_model_is_a_usage_error_naming_the_known_ones(tmp_path: Path) -> None:
    r = runner.invoke(app, ["models", "fetch", "yolo9000", "--config-dir", config_dir(tmp_path)])
    assert r.exit_code == 2 and "unknown model" in r.output and "yolox_s" in r.output


def test_verify_with_nothing_recorded_says_so(tmp_path: Path) -> None:
    r = runner.invoke(app, ["models", "verify", "--config-dir", config_dir(tmp_path)])
    assert r.exit_code == 0 and "no models recorded" in r.output


def test_register_verify_and_tamper_detection_round_trip(tmp_path: Path) -> None:
    cfg = config_dir(tmp_path)
    models = tmp_path / "var" / "models"
    models.mkdir(parents=True)
    (models / "mine.onnx").write_bytes(b"weights" * 200)

    reg = runner.invoke(
        app, ["models", "register", "mine.onnx", "--license", "MIT", "--config-dir", cfg]
    )
    assert reg.exit_code == 0 and "recorded mine.onnx sha256=" in reg.output
    assert (
        "registered locally" in runner.invoke(app, ["models", "list", "--config-dir", cfg]).output
    )
    assert runner.invoke(app, ["models", "verify", "--config-dir", cfg]).exit_code == 0

    (models / "mine.onnx").write_bytes(b"tampered" * 200)
    bad = runner.invoke(app, ["models", "verify", "--config-dir", cfg])
    assert bad.exit_code == 1 and "FAILED" in bad.output


def test_registering_a_missing_file_fails_cleanly(tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        [
            "models",
            "register",
            "ghost.onnx",
            "--license",
            "MIT",
            "--config-dir",
            config_dir(tmp_path),
        ],
    )
    assert r.exit_code == 1 and "not found" in r.output


# ------------------------------------------------------------------ run with a detector


def test_run_with_a_model_that_is_not_downloaded_names_the_fix(tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        [
            "run",
            "--source",
            "synthetic:5",
            "--detector",
            "yolox_s",
            "--duration",
            "0.5",
            "--config-dir",
            config_dir(tmp_path),
            "--log-level",
            "ERROR",
        ],
    )
    assert r.exit_code == 1 and "startup failed" in r.output
    assert "vigil models fetch yolox_s" in r.output


def test_run_with_a_detector_and_serve_runs_inference_and_serves_then_shuts_down(
    tmp_path: Path,
) -> None:
    cfg = config_dir(tmp_path)
    write_model(
        tmp_path / "var" / "models",
        name="fake_yolox.onnx",
        size=416,
        boxes=[PlantedBox(200, 150, 100, 80, 0, 0.9)],
    )
    port = free_port()
    r = runner.invoke(
        app,
        [
            "run",
            "--source",
            "synthetic:40",
            "--detector",
            "fake_yolox",
            "--device",
            "cpu",
            "--serve",
            "--port",
            str(port),
            "--duration",
            "2",
            "--status-interval",
            "1",
            "--config-dir",
            cfg,
            "--log-level",
            "ERROR",
        ],
    )
    assert r.exit_code == 0, r.output
    assert f"live viewer: http://127.0.0.1:{port}/" in r.output
    assert "inferred=40" in r.output and "detections=40" in r.output  # every frame, one person
    assert "shutdown clean" in r.output


def test_a_taken_port_is_a_clean_startup_failure(tmp_path: Path) -> None:
    cfg = config_dir(tmp_path)
    write_model(tmp_path / "var" / "models", name="fake_yolox.onnx", size=416)
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        r = runner.invoke(
            app,
            [
                "run",
                "--source",
                "synthetic:5",
                "--detector",
                "fake_yolox",
                "--device",
                "cpu",
                "--serve",
                "--port",
                str(port),
                "--duration",
                "1",
                "--config-dir",
                cfg,
                "--log-level",
                "ERROR",
            ],
        )
    assert r.exit_code == 1 and "already in use" in r.output


# ------------------------------------------------------------------ bench


@pytest.mark.parametrize(
    "args",
    [
        ["--resolution", "big"],
        ["--device", "tpu"],
        ["--source", "usb"],
        ["--resolution", "640x480x3"],
    ],
)
def test_bench_run_rejects_bad_arguments_as_usage_errors(tmp_path: Path, args: list[str]) -> None:
    r = runner.invoke(
        app,
        [
            "bench",
            "run",
            "--detector",
            "yolox_s",
            "--name",
            "x",
            *args,
            "--config-dir",
            config_dir(tmp_path),
        ],
    )
    assert r.exit_code == 2 and "--resolution WxH" in r.output


def test_bench_run_fails_loudly_when_the_model_is_missing(tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        [
            "bench",
            "run",
            "--detector",
            "yolox_s",
            "--device",
            "cpu",
            "--name",
            "x",
            "--duration",
            "6",
            "--window",
            "2",
            "--config-dir",
            config_dir(tmp_path),
        ],
    )
    assert r.exit_code == 1 and "benchmark failed" in r.output


def test_bench_run_rejects_an_invalid_spec(tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        [
            "bench",
            "run",
            "--detector",
            "yolox_s",
            "--name",
            "x",
            "--duration",
            "2",
            "--config-dir",
            config_dir(tmp_path),
        ],
    )
    assert r.exit_code == 1 and "benchmark failed" in r.output and "at least" in r.output


def test_a_short_real_benchmark_via_the_cli_saves_and_publishes_a_result(tmp_path: Path) -> None:
    cfg = config_dir(tmp_path)
    write_model(
        tmp_path / "var" / "models",
        name="fake_yolox.onnx",
        size=416,
        boxes=[PlantedBox(200, 150, 100, 80, 0, 0.9)],
    )
    r = runner.invoke(
        app,
        [
            "bench",
            "run",
            "--detector",
            "fake_yolox",
            "--device",
            "cpu",
            "--name",
            "cli-check",
            "--resolution",
            "320x240",
            "--source-fps",
            "20",
            "--infer-fps",
            "20",
            "--duration",
            "6",
            "--window",
            "2",
            "--publish",
            "--config-dir",
            cfg,
        ],
    )
    assert r.exit_code == 0, r.output
    assert "screening (under 120 s)" in r.output and "result: fps mean=" in r.output
    saved = list((tmp_path / "var" / "bench").glob("*-cli-check.json"))
    published = list((tmp_path / "docs" / "bench-results").glob("*-cli-check.json"))
    assert len(saved) == 1 and len(published) == 1

    rep = runner.invoke(app, ["bench", "report", "--config-dir", cfg])
    assert rep.exit_code == 0 and "from 1 stored result" in rep.output
    text = (tmp_path / "docs" / "PERFORMANCE.md").read_text(encoding="utf-8")
    assert "Screening runs" in text and "cli-check" in text  # a 6 s run is never "sustained"


def test_bench_report_with_no_results_writes_an_honest_empty_report(tmp_path: Path) -> None:
    r = runner.invoke(app, ["bench", "report", "--config-dir", config_dir(tmp_path)])
    assert r.exit_code == 0 and "from 0 stored result" in r.output
    assert "_None recorded yet._" in (tmp_path / "docs" / "PERFORMANCE.md").read_text(
        encoding="utf-8"
    )
