from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from vigil.config.loader import LoadedConfig, load_settings
from vigil.config.paths import resolve_paths

MakeConfig = Callable[..., LoadedConfig]


def test_relative_data_dir_resolves_against_the_config_parent_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_config: MakeConfig
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    paths = make_config({"paths.data_dir": "state"}).paths
    assert paths.data_dir == (tmp_path / "state").resolve()
    assert paths.logs_dir == paths.data_dir / "logs"
    assert paths.log_file == paths.logs_dir / "vigil.jsonl"
    assert paths.models_dir == paths.data_dir / "models"


def test_absolute_paths_are_honoured(tmp_path: Path, make_config: MakeConfig) -> None:
    target = (tmp_path / "abs-data").resolve()
    assert make_config({"paths.data_dir": str(target)}).paths.data_dir == target


def test_individual_directories_can_be_overridden(tmp_path: Path, make_config: MakeConfig) -> None:
    paths = make_config({"paths.logs_dir": "somewhere/logs", "logging.json_file": "my.jsonl"}).paths
    assert paths.logs_dir == (tmp_path / "somewhere/logs").resolve()
    assert paths.log_file == (tmp_path / "my.jsonl").resolve()


def test_ensure_directories_creates_everything(make_config: MakeConfig) -> None:
    paths = make_config().paths
    paths.ensure_directories()
    assert all(d.is_dir() for d in paths.directories())
    paths.ensure_directories()  # idempotent


def test_check_writable_reports_success_and_failure(
    tmp_path: Path, make_config: MakeConfig
) -> None:
    ok, err = make_config().paths.check_writable()
    assert ok and err is None
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    ok, err = make_config({"paths.data_dir": str(blocker / "child")}).paths.check_writable()
    assert not ok and err


def test_resolve_paths_is_pure(make_config: MakeConfig, tmp_path: Path) -> None:
    cfg = make_config()
    assert resolve_paths(cfg.settings, config_dir=tmp_path / "config") == cfg.paths


# ------------------------------------------------------------------ the committed config


def test_committed_config_loads_cleanly() -> None:
    cfg = load_settings(config_dir=REPO_ROOT / "config", env={}, use_dotenv=False)
    assert cfg.sources == ("defaults", "vigil.yaml")
    assert cfg.settings.vision.backend == "null"
    assert cfg.settings.modules.enabled == ()


def test_the_example_camera_is_disabled_so_no_device_is_opened_implicitly() -> None:
    cams = load_settings(config_dir=REPO_ROOT / "config", env={}, use_dotenv=False).settings.cameras
    assert cams and all(not c.enabled for c in cams)


_ABSOLUTE_PATH = re.compile(r"(?:^|[\s:'\"=])(?:[A-Za-z]:[\\/]|/(?:home|Users|c|mnt)/)")


def test_committed_config_has_no_machine_specific_paths() -> None:
    for file in (REPO_ROOT / "config").rglob("*.yaml"):
        text = file.read_text(encoding="utf-8")
        assert not _ABSOLUTE_PATH.search(text), f"absolute path in {file.name}"


def test_gitignore_keeps_local_overrides_and_secrets_out_of_git() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in ("var/", ".env", "config/vigil.local.yaml"):
        assert entry in ignored


def test_env_example_lists_secrets_with_empty_values() -> None:
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            assert line.split("=", 1)[1].strip() == "", f"placeholder must be empty: {line}"
