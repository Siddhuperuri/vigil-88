from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from vigil.config.loader import LoadedConfig, load_settings
from vigil.config.secrets import EnvResolver, parse_dotenv
from vigil.core.errors import ConfigError

MakeConfig = Callable[..., LoadedConfig]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load(tmp_path: Path, env: dict[str, str] | None = None, **kw: object) -> LoadedConfig:
    return load_settings(config_dir=tmp_path / "config", env=env or {}, use_dotenv=False, **kw)  # type: ignore[arg-type]


def test_defaults_load_with_no_files(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert cfg.sources == ("defaults",)
    assert cfg.settings.vision.backend == "null" and cfg.settings.cameras == ()


def test_layers_apply_in_order_main_then_local(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "vision: {max_batch_size: 2, device: cpu}\n")
    write(tmp_path / "config/vigil.local.yaml", "vision: {max_batch_size: 3}\n")
    s = load(tmp_path).settings
    assert s.vision.max_batch_size == 3  # local wins
    assert s.vision.device == "cpu"  # untouched key survives the merge


def test_full_precedence_defaults_main_local_env_overrides(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "logging: {level: DEBUG}\n")
    write(tmp_path / "config/vigil.local.yaml", "logging: {level: WARNING}\n")
    assert load(tmp_path).settings.logging.level == "WARNING"
    assert load(tmp_path, env={"VIGIL_LOGGING__LEVEL": "ERROR"}).settings.logging.level == "ERROR"
    both = load(tmp_path, env={"VIGIL_LOGGING__LEVEL": "ERROR"}, overrides={"logging.level": "INFO"})
    assert both.settings.logging.level == "INFO"
    assert both.sources[-2:] == ("environment", "overrides")


def test_env_overrides_are_nested_and_typed(tmp_path: Path) -> None:
    s = load(
        tmp_path,
        env={"VIGIL_VISION__MAX_BATCH_SIZE": "2", "VIGIL_VISION__ALLOW_CPU_FALLBACK": "false"},
    ).settings
    assert s.vision.max_batch_size == 2 and s.vision.allow_cpu_fallback is False


def test_single_segment_vigil_variables_are_not_overrides(tmp_path: Path) -> None:
    load(tmp_path, env={"VIGIL_CONFIG_DIR": "x", "VIGIL_API_TOKEN": "t", "VIGIL_ODD": "1"})


def test_unrelated_environment_is_ignored(tmp_path: Path) -> None:
    load(tmp_path, env={"PATH": "x", "HOME": "y", "OTHER__THING": "1"})


def test_lists_replace_rather_than_merge(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "modules: {enabled: [a, b]}\n")
    write(tmp_path / "config/vigil.local.yaml", "modules: {enabled: [c]}\n")
    assert load(tmp_path).settings.modules.enabled == ("c",)


# ------------------------------------------------------------------ interpolation


def test_env_interpolation_with_value_default_and_reference(tmp_path: Path) -> None:
    write(
        tmp_path / "config/vigil.yaml",
        "paths: {data_dir: '${env:DATA_ROOT}/run'}\n"
        "logging: {level: '${env:LVL:-WARNING}'}\n"
        "vision: {weights: '${paths.data_dir}/w.pt'}\n",
    )
    s = load(tmp_path, env={"DATA_ROOT": "/data"}).settings
    assert s.paths.data_dir == Path("/data/run")
    assert s.logging.level == "WARNING"
    assert s.vision.weights == "/data/run/w.pt"


def test_missing_env_variable_is_a_named_error_without_a_default(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "vision: {weights: '${env:NOPE_NOT_SET}'}\n")
    with pytest.raises(ConfigError, match="NOPE_NOT_SET"):
        load(tmp_path)


def test_unresolvable_and_circular_references_fail(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "vision: {weights: '${nowhere.at.all}'}\n")
    with pytest.raises(ConfigError, match="does not resolve"):
        load(tmp_path)
    write(tmp_path / "config/vigil.yaml",
          "vision: {weights: '${vision.device}'}\n")  # resolves, but then loops below
    write(tmp_path / "config/vigil.local.yaml",
          "vision: {device: '${vision.weights}'}\n")
    with pytest.raises(ConfigError, match="circular|deep"):
        load(tmp_path)


# ------------------------------------------------------------------ includes


def test_include_expands_files_in_sorted_order(tmp_path: Path) -> None:
    for name in ("b-cam", "a-cam"):
        write(
            tmp_path / f"config/cameras/{name}.yaml",
            f"camera_id: {name}\nsource: {{kind: synthetic}}\n",
        )
    write(tmp_path / "config/vigil.yaml", "cameras:\n  - $include: cameras/*.yaml\n")
    ids = [c.camera_id for c in load(tmp_path).settings.cameras]
    assert ids == ["a-cam", "b-cam"]


def test_include_matching_nothing_yields_no_cameras(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "cameras:\n  - $include: cameras/*.yaml\n")
    assert load(tmp_path).settings.cameras == ()


@pytest.mark.parametrize("pattern", ["../secrets/*.yaml", "/etc/*.yaml", "a/../../b/*.yaml"])
def test_include_cannot_escape_the_config_dir(tmp_path: Path, pattern: str) -> None:
    write(tmp_path / "config/vigil.yaml", f"cameras:\n  - $include: '{pattern}'\n")
    with pytest.raises(ConfigError, match="relative"):
        load(tmp_path)


# ------------------------------------------------------------------ validation


def test_unknown_key_is_an_error_naming_the_path(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "temporal: {l_activte: 3}\n")
    with pytest.raises(ConfigError) as e:
        load(tmp_path)
    assert any("temporal.l_activte" in i for i in e.value.issues)


def test_all_problems_are_reported_together(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "vision: {max_batch_size: 0}\nlogging: {level: LOUD}\n")
    with pytest.raises(ConfigError) as e:
        load(tmp_path)
    joined = " ".join(e.value.issues)
    assert "vision.max_batch_size" in joined and "logging.level" in joined


def test_rejected_secret_values_are_never_echoed(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "api: {bind_host: 0.0.0.0, auth: {enabled: true, token: short-secret-xyz}}\n")
    with pytest.raises(ConfigError) as e:
        load(tmp_path)
    assert "short-secret-xyz" not in str(e.value)


def test_bad_yaml_and_wrong_top_level_type(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "a: [unclosed\n")
    with pytest.raises(ConfigError, match="cannot read"):
        load(tmp_path)
    write(tmp_path / "config/vigil.yaml", "- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load(tmp_path)


def test_empty_yaml_file_is_fine(tmp_path: Path) -> None:
    write(tmp_path / "config/vigil.yaml", "")
    assert load(tmp_path).settings.version == 1


# ------------------------------------------------------------------ dotenv and hash


def test_parse_dotenv() -> None:
    parsed = parse_dotenv(
        "# comment\n\nA=1\nexport B = two \nC=\"quoted value\"\nD='single'\nE=x # trailing\nbad line\n"
    )
    assert parsed == {"A": "1", "B": "two", "C": "quoted value", "D": "single", "E": "x"}


def test_real_environment_beats_dotenv() -> None:
    r = EnvResolver({"X": "real"}, {"X": "file", "Y": "file"})
    assert r.get("X") == "real" and r.get("Y") == "file" and r.get("Z") is None
    assert r.merged() == {"X": "real", "Y": "file"}


def test_dotenv_file_feeds_interpolation_but_not_os_environ(tmp_path: Path) -> None:
    write(tmp_path / ".env", "DATA_ROOT=/from-dotenv\n")
    write(tmp_path / "config/vigil.yaml", "paths: {data_dir: '${env:DATA_ROOT}'}\n")
    cfg = load_settings(config_dir=tmp_path / "config", env={})
    assert cfg.settings.paths.data_dir == Path("/from-dotenv")
    cfg2 = load_settings(config_dir=tmp_path / "config", env={"DATA_ROOT": "/real"})
    assert cfg2.settings.paths.data_dir == Path("/real")


def test_config_hash_is_stable_and_changes_with_config(tmp_path: Path) -> None:
    a = load(tmp_path).settings
    assert a.config_hash() == load(tmp_path).settings.config_hash()
    b = load(tmp_path, overrides={"vision.max_batch_size": 2}).settings
    assert a.config_hash() != b.config_hash()


def test_config_hash_never_contains_a_secret(tmp_path: Path) -> None:
    token = "t" * 40
    s = load(
        tmp_path,
        overrides={"api.bind_host": "0.0.0.0", "api.auth.enabled": True, "api.auth.token": token},  # noqa: S104
    ).settings
    assert token not in repr(s) and token not in s.model_dump_json()
    s2 = load(
        tmp_path,
        overrides={"api.bind_host": "0.0.0.0", "api.auth.enabled": True, "api.auth.token": "u" * 40},  # noqa: S104
    ).settings
    assert s.config_hash() == s2.config_hash()  # the secret is not part of the identity


def test_config_dir_from_environment_variable(tmp_path: Path) -> None:
    write(tmp_path / "elsewhere/vigil.yaml", "vision: {max_batch_size: 7}\n")
    cfg = load_settings(env={"VIGIL_CONFIG_DIR": str(tmp_path / "elsewhere")}, use_dotenv=False)
    assert cfg.settings.vision.max_batch_size == 7
