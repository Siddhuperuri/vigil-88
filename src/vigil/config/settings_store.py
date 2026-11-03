"""The console-managed settings layer, and backups of every file the console replaces.

`config/vigil.local.yaml` is already the loader's second layer, applied over `vigil.yaml`. The
console writes there and nowhere else in the main configuration, and only the sections on the
allowlist below: a hand-edited `vigil.yaml` is never rewritten, so a person's comments and layout
survive. Whatever else is in the local file is preserved as it was.

Every write that replaces an existing file first copies it to `config/backups/`, named by the time
and what it was, and the folder is pruned to the newest `KEEP` entries. There is no clock read
here: the caller passes the stamp (the application's clock), so this stays deterministic.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import yaml

from vigil.config.loader import LOCAL_FILE
from vigil.config.writer import atomic_write
from vigil.core.errors import ConfigError

BACKUPS_DIR = "backups"
KEEP = 50
MAX_LOCAL_BYTES = 256 * 1024
# The only top-level sections the console may write into vigil.local.yaml.
WRITABLE_SECTIONS = frozenset({"alerts"})
_HEADER = (
    "# Written by the VIGIL-88 console (settings it manages). Safe to edit by hand.\n"
    "# Previous versions are kept in backups/.\n"
)


def local_path(config_dir: Path) -> Path:
    return config_dir / LOCAL_FILE


def read_local(config_dir: Path) -> dict[str, object]:
    path = local_path(config_dir)
    if not path.is_file():
        return {}
    try:
        if path.stat().st_size > MAX_LOCAL_BYTES:
            raise ConfigError(f"{LOCAL_FILE} is larger than {MAX_LOCAL_BYTES // 1024} KiB")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {LOCAL_FILE}", issues=[str(exc)]) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{LOCAL_FILE} must contain a mapping at the top level")
    return cast(dict[str, object], raw)


def backup(config_dir: Path, path: Path, stamp: str, *, group: str | None = None) -> Path | None:
    """Copy `path` into backups/ before it is replaced. None if there was nothing to copy."""
    if not path.is_file():
        return None
    folder = config_dir / BACKUPS_DIR / (group or stamp)
    folder.mkdir(parents=True, exist_ok=True)
    relative = path.relative_to(config_dir)
    target = folder / "__".join(relative.parts)
    shutil.copy2(path, target)
    prune(config_dir)
    return target


def prune(config_dir: Path) -> None:
    root = config_dir / BACKUPS_DIR
    if not root.is_dir():
        return
    entries = sorted(root.iterdir(), key=lambda p: p.name)
    for old in entries[:-KEEP]:
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)


def write_local_section(
    config_dir: Path,
    section: str,
    value: dict[str, object],
    *,
    stamp: str,
    make_backup: bool = True,
) -> Path:
    """Replace one allowlisted section of vigil.local.yaml, keeping every other key as it was."""
    if section not in WRITABLE_SECTIONS:
        raise ConfigError(f"the console may not write the {section!r} section")
    path = local_path(config_dir)
    current = read_local(config_dir)
    if make_backup:
        backup(config_dir, path, stamp)
    current[section] = value
    atomic_write(path, _HEADER + yaml.safe_dump(current, sort_keys=False, allow_unicode=True))
    return path
