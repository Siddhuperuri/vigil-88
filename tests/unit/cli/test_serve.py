"""`vigil serve`: the engine as a service (ADR 0016). The desktop shell depends on three
properties — it starts empty, it picks its own port, and stdout is machine-readable."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vigil.cli.main import app

runner = CliRunner()


def _config(tmp_path: Path) -> str:
    cfg = tmp_path / "config"
    cfg.mkdir()
    return str(cfg)


def _events(stdout: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def test_serve_starts_with_no_cameras_and_reports_the_port_it_was_given(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "serve",
            "--config-dir",
            _config(tmp_path),
            "--port",
            "0",
            "--duration",
            "1",
            "--log-level",
            "ERROR",
        ],
    )
    assert result.exit_code == 0, result.output
    events = _events(result.stdout)
    ready = next(e for e in events if e["event"] == "ready")
    assert ready["cameras"] == 0
    assert isinstance(ready["port"], int) and ready["port"] > 0
    assert ready["url"] == f"http://127.0.0.1:{ready['port']}"
    assert events[-1] == {**events[-1], "event": "stopped", "clean": True}


def test_serve_ready_comes_before_health_and_stopped_comes_last(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "serve",
            "--config-dir",
            _config(tmp_path),
            "--port",
            "0",
            "--duration",
            "1",
            "--log-level",
            "ERROR",
        ],
    )
    names = [e["event"] for e in _events(result.stdout)]
    assert names[0] == "ready" and names[-1] == "stopped"
    assert "health" in names


def test_run_still_refuses_to_start_with_no_cameras(tmp_path: Path) -> None:
    """`run` keeps its guard: it is the command a person runs expecting something to be watched."""
    result = runner.invoke(app, ["run", "--config-dir", _config(tmp_path), "--duration", "1"])
    assert result.exit_code == 2 and "no enabled cameras" in result.output


def test_serve_reports_a_taken_port_as_a_failed_event_and_a_clean_exit_code(
    tmp_path: Path,
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        result = runner.invoke(
            app,
            [
                "serve",
                "--config-dir",
                _config(tmp_path),
                "--port",
                str(port),
                "--duration",
                "1",
                "--log-level",
                "ERROR",
            ],
        )
    assert result.exit_code == 1
    failed = [e for e in _events(result.stdout) if e["event"] == "failed"]
    assert failed and failed[0]["stage"] == "startup"
    assert not any(e["event"] == "ready" for e in _events(result.stdout))


def test_serve_with_a_bad_config_fails_with_a_usage_code_and_an_event(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    (Path(cfg) / "vigil.yaml").write_text(
        "version: 1\napi:\n  bind_port: 99999\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["serve", "--config-dir", cfg, "--duration", "1"])
    assert result.exit_code == 2
    assert any(e["event"] == "failed" and e["stage"] == "config" for e in _events(result.stdout))


# ------------------------------------------------------------------ accounts (ADR 0016)


def test_serve_says_whether_setup_is_pending(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "serve",
            "--config-dir",
            _config(tmp_path),
            "--port",
            "0",
            "--duration",
            "1",
            "--log-level",
            "ERROR",
        ],
    )
    ready = next(e for e in _events(result.stdout) if e["event"] == "ready")
    assert ready["setup_required"] is True


def test_headless_serve_shows_a_setup_token_once_on_stderr_never_on_stdout(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "serve",
            "--config-dir",
            _config(tmp_path),
            "--port",
            "0",
            "--duration",
            "1",
            "--log-level",
            "ERROR",
        ],
    )
    assert "setup token (shown once):" in result.stderr
    assert "setup token" not in result.stdout  # stdout is the parent's channel, not a log
    token = result.stderr.split("setup token (shown once):")[1].split()[0]
    assert token not in result.stdout


def test_a_token_from_the_parent_is_used_and_not_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIGIL_SETUP_TOKEN", "from-the-shell-0123456789abcdef")
    result = runner.invoke(
        app,
        [
            "serve",
            "--config-dir",
            _config(tmp_path),
            "--port",
            "0",
            "--duration",
            "1",
            "--log-level",
            "ERROR",
        ],
    )
    assert "from-the-shell" not in result.output and "setup token" not in result.stderr


def _seed_admin(config: str) -> None:
    from vigil.auth.factory import build_auth_service
    from vigil.config.loader import load_settings
    from vigil.core.clock import SystemClock

    loaded = load_settings(config_dir=Path(config))
    service, store = build_auth_service(
        loaded.settings, loaded.paths, SystemClock(), setup_token="t" * 30
    )
    service.setup(token="t" * 30, username="alice", password="correct horse battery")
    store.close()


def test_reset_admin_needs_the_typed_phrase(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed_admin(cfg)
    refused = runner.invoke(app, ["auth", "reset-admin", "--config-dir", cfg, "--confirm", "yes"])
    assert refused.exit_code == 1 and "Nothing was changed" in refused.stderr


def test_reset_admin_removes_administrators_and_requires_setup_again(tmp_path: Path) -> None:
    from vigil.auth.factory import build_auth_service
    from vigil.config.loader import load_settings
    from vigil.core.clock import SystemClock

    cfg = _config(tmp_path)
    _seed_admin(cfg)
    done = runner.invoke(
        app, ["auth", "reset-admin", "--config-dir", cfg, "--confirm", "RESET ADMINISTRATOR"]
    )
    assert done.exit_code == 0, done.output
    loaded = load_settings(config_dir=Path(cfg))
    service, store = build_auth_service(loaded.settings, loaded.paths, SystemClock())
    try:
        assert service.setup_required()
        assert any(e.action == "admin.reset" for e in service.audit.entries())
    finally:
        store.close()
