"""Locating and loading a camera's calibration file.

The same shape as `config/zones.py`, for the same reason: a camera names its calibration with
`calibration_ref`, a short slug, never a path, so the file is
`<config_dir>/calibration/<calibration_ref>.calib.yaml` and a camera cannot reach outside the
config directory however its configuration is written.

Failure is a `ConfigError` listing every problem. The caller decides what that means; for
calibration it means the camera runs without `GROUND_PLANE` and reports velocity in px/s, which
is what it did before anyone calibrated anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from vigil.config.schema.calibration import CALIBRATION_REF_PATTERN, CalibrationConfig
from vigil.core.errors import ConfigError

CALIBRATION_DIR = "calibration"
CALIBRATION_SUFFIX = ".calib.yaml"
MAX_CALIBRATION_FILE_BYTES = 64 * 1024


def calibration_file_path(config_dir: Path, calibration_ref: str) -> Path:
    if not CALIBRATION_REF_PATTERN.match(calibration_ref):
        raise ConfigError(
            f"invalid calibration_ref {calibration_ref!r}: use a short name such as 'lobby-floor', "
            f"not a path (it selects config/{CALIBRATION_DIR}/<name>{CALIBRATION_SUFFIX})"
        )
    return config_dir / CALIBRATION_DIR / f"{calibration_ref}{CALIBRATION_SUFFIX}"


def load_calibration(config_dir: Path, calibration_ref: str) -> CalibrationConfig:
    path = calibration_file_path(config_dir, calibration_ref)
    try:
        if path.stat().st_size > MAX_CALIBRATION_FILE_BYTES:
            raise ConfigError(
                f"calibration file {path.name} is larger than "
                f"{MAX_CALIBRATION_FILE_BYTES // 1024} KiB"
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"calibration file not found: config/{CALIBRATION_DIR}/{path.name}",
            issues=[f"calibration_ref: {calibration_ref}"],
        ) from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read calibration file {path.name}", issues=[str(exc)]) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"calibration file {path.name} must contain a mapping at the top level")
    try:
        return CalibrationConfig.model_validate(cast(dict[str, object], raw))
    except ValidationError as exc:
        issues = [
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ConfigError(f"invalid calibration file {path.name}", issues=issues) from None
