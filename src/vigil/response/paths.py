"""Where evidence lives, and the guard that keeps it there (01 §10, 05 §6).

    var/evidence/<camera_id>/<YYYY>/<MM>/<DD>/<incident_id>/

Date-partitioned under the camera so retention deletion is a directory walk with no database
query, and so a human browsing the filesystem can navigate it.

Every component of that path is code-controlled — a validated camera slug, a ULID, and a date —
so a traversal should be impossible by construction. `safe_join` checks anyway, because "should
be impossible by construction" is a statement about today's callers, and the one that matters is
the caller added next year. Both halves are enforced: each component must match a strict
pattern, **and** the resolved path must still be inside the root after symlinks are resolved.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from vigil.core.errors import PersistenceError

# One path component: no separators, no `..`, no drive letters, no leading dot.
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EVIDENCE_MANIFEST = "manifest.json"


def safe_component(value: str, *, label: str) -> str:
    if not SAFE_COMPONENT.match(value) or ".." in value:
        raise PersistenceError(
            f"unsafe {label} for an evidence path: {value!r}",
            context={"label": label, "value": value},
        )
    return value


def safe_join(root: Path, *components: str) -> Path:
    """Join `components` under `root`, refusing anything that could escape it."""
    for component in components:
        safe_component(component, label="path component")
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*components)
    # `strict=False`: the directory does not exist yet the first time this is called.
    resolved = candidate.resolve(strict=False)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PersistenceError(
            "an evidence path resolved outside the evidence directory",
            context={"root": str(resolved_root), "resolved": str(resolved)},
        )
    return resolved


def incident_relative_dir(camera_id: str, incident_id: str, confirmed_wall_utc: datetime) -> str:
    """The POSIX-style relative directory stored on each `Evidence` row.

    Relative and POSIX-separated so the data directory is movable between machines and between
    operating systems; `Evidence.relative_path` refuses an absolute path or a `..`.
    """
    safe_component(camera_id, label="camera_id")
    safe_component(incident_id, label="incident_id")
    day = confirmed_wall_utc.strftime("%Y/%m/%d")
    return f"{camera_id}/{day}/{incident_id}"


def incident_dir(
    evidence_root: Path, camera_id: str, incident_id: str, confirmed: datetime
) -> Path:
    parts = incident_relative_dir(camera_id, incident_id, confirmed).split("/")
    return safe_join(evidence_root, *parts)


def resolve_relative(evidence_root: Path, relative_path: str) -> Path:
    """Turn a stored `relative_path` back into an absolute one, refusing an escape.

    This is what the API calls before serving a file. The path came out of our own database, but
    a database is still input: if a row were ever tampered with, this is the check that stops it
    becoming an arbitrary file read.
    """
    if not relative_path or relative_path.startswith(("/", "\\")):
        raise PersistenceError(f"stored evidence path is not relative: {relative_path!r}")
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p]
    if not parts:
        raise PersistenceError("stored evidence path is empty")
    return safe_join(evidence_root, *parts)
