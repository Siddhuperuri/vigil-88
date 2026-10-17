"""Locating and loading a camera's zone file.

A camera names its zone set with `zones_ref`, a short slug, never a path. The file is
`<config_dir>/zones/<zones_ref>.zones.yaml`, so a camera cannot reach outside the config
directory however its configuration is written. Failure is reported as a `ConfigError` that
lists every problem; the caller decides what that means (for zones: the camera runs without
the ZONES capability, it does not stop the application).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from vigil.config.schema.zones import ZONES_REF_PATTERN, ZoneSetConfig
from vigil.core.errors import ConfigError

ZONES_DIR = "zones"
ZONES_SUFFIX = ".zones.yaml"
MAX_ZONE_FILE_BYTES = 256 * 1024


def zone_file_path(config_dir: Path, zones_ref: str) -> Path:
    if not ZONES_REF_PATTERN.match(zones_ref):
        raise ConfigError(
            f"invalid zones_ref {zones_ref!r}: use a short name such as 'front-gate', "
            f"not a path (it selects config/{ZONES_DIR}/<name>{ZONES_SUFFIX})"
        )
    return config_dir / ZONES_DIR / f"{zones_ref}{ZONES_SUFFIX}"


def load_zone_set(config_dir: Path, zones_ref: str) -> ZoneSetConfig:
    path = zone_file_path(config_dir, zones_ref)
    try:
        if path.stat().st_size > MAX_ZONE_FILE_BYTES:
            raise ConfigError(
                f"zone file {path.name} is larger than {MAX_ZONE_FILE_BYTES // 1024} KiB"
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"zone file not found: config/{ZONES_DIR}/{path.name}",
            issues=[f"zones_ref: {zones_ref}"],
        ) from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read zone file {path.name}", issues=[str(exc)]) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"zone file {path.name} must contain a mapping at the top level")
    try:
        return ZoneSetConfig.model_validate(cast(dict[str, object], raw))
    except ValidationError as exc:
        issues = [
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ConfigError(f"invalid zone file {path.name}", issues=issues) from None
