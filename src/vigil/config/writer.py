"""Writing camera and zone files as a person would, but never producing one the loader rejects.

Every write goes through the same pydantic models the loader uses, so a file this module writes is
one the engine will accept; a value that would not validate is refused here, before it reaches the
disk. Writes are atomic (temp file in the same folder, then `os.replace`), so a crash or a full disk
leaves the old file intact rather than half of a new one.

The camera file holds **no secret**. `SourceConfig` already refuses inline credentials, and the
only credential-shaped fields are the *names* of an environment variable or a stored credential.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from vigil.config.schema.camera import CameraConfig
from vigil.config.schema.zones import ZoneSetConfig
from vigil.config.zones import ZONES_DIR, ZONES_SUFFIX, zone_file_path
from vigil.core.errors import ConfigError
from vigil.core.ids import validate_camera_id

CAMERAS_DIR = "cameras"
MAX_CAMERA_FILE_BYTES = 64 * 1024
_HEADER = "# Written by the VIGIL-88 console. Safe to edit by hand; the console re-reads it.\n"


def camera_file_path(config_dir: Path, camera_id: str) -> Path:
    """`config/cameras/<id>.yaml`. The id is a validated slug, so it cannot name another folder."""
    validate_camera_id(camera_id)
    return config_dir / CAMERAS_DIR / f"{camera_id}.yaml"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)  # never leave a stray temp file behind
        raise


def _dump(model: CameraConfig | ZoneSetConfig) -> str:
    data = model.model_dump(mode="json", exclude_defaults=True)
    return _HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _validated(raw: object, model: type[CameraConfig] | type[ZoneSetConfig], what: str):  # type: ignore[no-untyped-def]
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        issues = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(f"{what} is not valid", issues=issues) from None


def validate_camera(raw: object) -> CameraConfig:
    result = _validated(raw, CameraConfig, "the camera")
    assert isinstance(result, CameraConfig)
    return result


def validate_zone_set(raw: object) -> ZoneSetConfig:
    result = _validated(raw, ZoneSetConfig, "the zone set")
    assert isinstance(result, ZoneSetConfig)
    return result


# ------------------------------------------------------------------ cameras


def write_camera(config_dir: Path, camera: CameraConfig) -> Path:
    path = camera_file_path(config_dir, camera.camera_id)
    text = _dump(camera)
    if len(text.encode("utf-8")) > MAX_CAMERA_FILE_BYTES:
        raise ConfigError("the camera file would be unreasonably large")
    _atomic_write(path, text)
    return path


def read_camera(config_dir: Path, camera_id: str) -> CameraConfig | None:
    path = camera_file_path(config_dir, camera_id)
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_CAMERA_FILE_BYTES:
            raise ConfigError(f"camera file {path.name} is too large")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read camera file {path.name}", issues=[str(exc)]) from exc
    return validate_camera(raw)


def list_camera_files(config_dir: Path) -> list[str]:
    folder = config_dir / CAMERAS_DIR
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.yaml") if p.is_file())


def delete_camera(config_dir: Path, camera_id: str) -> bool:
    path = camera_file_path(config_dir, camera_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


# ------------------------------------------------------------------ zones


def write_zone_set(config_dir: Path, ref: str, zones: ZoneSetConfig) -> Path:
    path = zone_file_path(config_dir, ref)  # validates the ref as a slug
    _atomic_write(path, _dump(zones))
    return path


def delete_zone_set(config_dir: Path, ref: str) -> bool:
    path = zone_file_path(config_dir, ref)
    if not path.is_file():
        return False
    path.unlink()
    return True


def zone_refs(config_dir: Path) -> list[str]:
    folder = config_dir / ZONES_DIR
    if not folder.is_dir():
        return []
    return sorted(p.name.removesuffix(ZONES_SUFFIX) for p in folder.glob(f"*{ZONES_SUFFIX}"))
