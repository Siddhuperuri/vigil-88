"""Loading a module's parameter file: `<config_dir>/modules/<module_id>.yaml` (06 §5).

Parameters resolve in three levels, innermost last:

    1. `default_params` from the module's descriptor
    2. `config/modules/<module_id>.yaml`          -- this file
    3. `cameras/<cam>.yaml` -> `module_params[<module_id>]`

The merge is shallow, because every parameter is a scalar or a flat list; a nested merge would
make it impossible to *remove* an inherited list entry.

Validation against the module's own model happens in `bind()`, not here. This loader only
answers "what did the operator write", and an unknown key is an error there rather than a
warning, because a silently-ignored typo in a threshold is indistinguishable from the module
not working.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml

from vigil.config.schema.zones import ZONE_ID_PATTERN
from vigil.core.errors import ConfigError

MODULES_DIR = "modules"
MODULE_PARAMS_SUFFIX = ".yaml"
MAX_PARAMS_FILE_BYTES = 64 * 1024

ParamMap = Mapping[str, object]


def module_params_path(config_dir: Path, module_id: str) -> Path:
    """A module_id is a short slug, never a path, so a module cannot reach outside config/."""
    if not ZONE_ID_PATTERN.match(module_id):
        raise ConfigError(
            f"invalid module_id {module_id!r}: use a short name such as 'intrusion', not a path "
            f"(it selects config/{MODULES_DIR}/<name>{MODULE_PARAMS_SUFFIX})"
        )
    return config_dir / MODULES_DIR / f"{module_id}{MODULE_PARAMS_SUFFIX}"


def load_module_params(config_dir: Path, module_id: str) -> ParamMap:
    """Read one module's parameter file. A missing file means "no overrides", not an error:
    a module that is happy with its defaults should not need a file to say so."""
    path = module_params_path(config_dir, module_id)
    try:
        if path.stat().st_size > MAX_PARAMS_FILE_BYTES:
            raise ConfigError(
                f"module parameter file {path.name} is larger than "
                f"{MAX_PARAMS_FILE_BYTES // 1024} KiB"
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(
            f"cannot read module parameter file {path.name}", issues=[str(exc)]
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"module parameter file {path.name} must contain a mapping of parameter names "
            "to values at the top level"
        )
    mapping = cast(dict[str, object], raw)
    if any(not isinstance(key, str) for key in mapping):
        raise ConfigError(f"module parameter file {path.name} has a non-string parameter name")
    return mapping


def merge_params(*layers: ParamMap) -> dict[str, object]:
    """Shallow merge, last layer winning. Order is defaults, file, per-camera."""
    merged: dict[str, object] = {}
    for layer in layers:
        merged.update(layer)
    return merged
