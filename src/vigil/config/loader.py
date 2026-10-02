"""Layered configuration loading (01 §7.1).

Precedence, last wins:

    code defaults -> config/vigil.yaml -> config/vigil.local.yaml
                  -> VIGIL_* environment (and .env) -> explicit overrides (CLI)

Environment overrides use `VIGIL_<SECTION>__<KEY>`; a name with no `__` is not an override.
Strings may reference `${env:NAME}`, `${env:NAME:-default}` or another key `${paths.data_dir}`.
Validation happens once; an invalid configuration is a startup failure that lists every
problem and never echoes a rejected value (it may be a secret).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import yaml
from pydantic import ValidationError

from vigil.config.paths import ResolvedPaths, resolve_paths
from vigil.config.secrets import EnvResolver, read_dotenv
from vigil.config.settings import Settings
from vigil.core.errors import ConfigError

CONFIG_DIR_ENV = "VIGIL_CONFIG_DIR"
MAIN_FILE = "vigil.yaml"
LOCAL_FILE = "vigil.local.yaml"
ENV_PREFIX = "VIGIL_"
NESTING = "__"
INCLUDE_KEY = "$include"
MAX_INTERPOLATION_DEPTH = 10
_REF_RE = re.compile(r"\$\{([^}]+)\}")

RawDict = dict[str, object]


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    settings: Settings
    paths: ResolvedPaths
    sources: tuple[str, ...]


def load_settings(
    *,
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
    use_dotenv: bool = True,
) -> LoadedConfig:
    real_env: Mapping[str, str] = os.environ if env is None else env
    if config_dir is None:
        configured = real_env.get(CONFIG_DIR_ENV)
        config_dir = Path(configured) if configured else Path.cwd() / "config"
    config_dir = config_dir.resolve()
    base_dir = config_dir.parent

    dotenv = read_dotenv(base_dir / ".env") if use_dotenv else {}
    resolver = EnvResolver(real_env, dotenv)

    sources: list[str] = ["defaults"]
    raw: RawDict = {}
    for name in (MAIN_FILE, LOCAL_FILE):
        file = config_dir / name
        if file.is_file():
            raw = _deep_merge(raw, _read_yaml_mapping(file))
            sources.append(str(file.name))
    env_layer = _env_overrides(resolver.merged())
    if env_layer:
        raw = _deep_merge(raw, env_layer)
        sources.append("environment")
    if overrides:
        raw = _deep_merge(raw, _dotted_to_nested(overrides))
        sources.append("overrides")

    raw = cast(RawDict, _expand_includes(raw, config_dir))
    raw = cast(RawDict, _interpolate(raw, raw, resolver))
    settings = _validate(raw)
    return LoadedConfig(settings, resolve_paths(settings, config_dir=config_dir), tuple(sources))


# ------------------------------------------------------------------ layers


def _read_yaml_mapping(path: Path) -> RawDict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read configuration file {path.name}", issues=[str(exc)]) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")
    return cast(RawDict, data)


def _deep_merge(base: RawDict, top: Mapping[str, object]) -> RawDict:
    out: RawDict = dict(base)
    for key, value in top.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            out[key] = _deep_merge(cast(RawDict, existing), cast(RawDict, value))
        else:
            out[key] = value
    return out


def _nest(path: list[str], value: object) -> RawDict:
    node: object = value
    for key in reversed(path):
        node = {key: node}
    return cast(RawDict, node)


def _env_overrides(env: Mapping[str, str]) -> RawDict:
    out: RawDict = {}
    for name in sorted(env):
        if not name.startswith(ENV_PREFIX) or NESTING not in name:
            continue
        path = [p for p in name[len(ENV_PREFIX) :].lower().split(NESTING) if p]
        if not path:
            continue
        out = _deep_merge(out, _nest(path, _parse_scalar(env[name])))
    return out


def _parse_scalar(text: str) -> object:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def _dotted_to_nested(overrides: Mapping[str, object]) -> RawDict:
    out: RawDict = {}
    for key, value in overrides.items():
        out = _deep_merge(out, _nest(key.split("."), value))
    return out


# ------------------------------------------------------------------ includes


def _expand_includes(node: object, config_dir: Path) -> object:
    if isinstance(node, dict):
        return {k: _expand_includes(v, config_dir) for k, v in node.items()}
    if isinstance(node, list):
        out: list[object] = []
        for item in node:
            if isinstance(item, dict) and set(item) == {INCLUDE_KEY}:
                out.extend(_load_include(str(item[INCLUDE_KEY]), config_dir))
            else:
                out.append(_expand_includes(item, config_dir))
        return out
    return node


def _load_include(pattern: str, config_dir: Path) -> list[object]:
    p = PurePosixPath(pattern)
    if p.is_absolute() or ".." in p.parts or Path(pattern).drive:
        raise ConfigError(
            f"$include pattern {pattern!r} must be relative to the config dir with no '..'"
        )
    docs: list[object] = []
    for file in sorted(config_dir.glob(pattern)):
        if file.is_file():
            docs.append(_read_yaml_mapping(file))
    return docs


# ------------------------------------------------------------------ interpolation


def _interpolate(
    node: object, root: RawDict, resolver: EnvResolver, *, _path: str = "", _depth: int = 0
) -> object:
    if isinstance(node, dict):
        return {
            k: _interpolate(v, root, resolver, _path=f"{_path}.{k}".lstrip("."), _depth=_depth)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [
            _interpolate(v, root, resolver, _path=f"{_path}[{i}]", _depth=_depth)
            for i, v in enumerate(node)
        ]
    if isinstance(node, str) and "${" in node:
        return _interpolate_string(node, root, resolver, _path, _depth)
    return node


def _interpolate_string(
    text: str, root: RawDict, resolver: EnvResolver, where: str, depth: int
) -> str:
    if depth > MAX_INTERPOLATION_DEPTH:
        raise ConfigError(f"interpolation too deep or circular at {where}")

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        if expr.startswith("env:"):
            name, sep, default = expr[4:].partition(":-")
            value = resolver.get(name)
            if value is None:
                if sep:
                    return default
                raise ConfigError(
                    f"environment variable {name!r} referenced at {where} is not set"
                )
            return value
        target = _lookup(root, expr)
        if target is None:
            raise ConfigError(f"reference ${{{expr}}} at {where} does not resolve to a value")
        return _interpolate_string(str(target), root, resolver, where, depth + 1)

    return _REF_RE.sub(replace, text)


def _lookup(root: RawDict, dotted: str) -> object | None:
    node: object = root
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# ------------------------------------------------------------------ validation


def _validate(raw: RawDict) -> Settings:
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        issues = [_format_issue(err["loc"], err["msg"]) for err in exc.errors()]
        raise ConfigError("invalid configuration", issues=issues) from None


def _format_issue(loc: tuple[int | str, ...], msg: str) -> str:
    where = ".".join(str(p) for p in loc) or "(root)"
    return f"{where}: {msg}"
