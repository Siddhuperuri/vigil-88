"""What an operator needs to judge whether the machine is healthy: recent logs and storage.

Both are read-only and bounded. The log tail reads at most `TAIL_BYTES` from the end of the file,
so a log of any size costs the same; storage walks the evidence folder with a ceiling on entries and
says `truncated` when it bites, rather than stalling a request on a huge tree. No absolute path
leaves the machine: a client is told sizes and file names, never where the data directory is.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from vigil.api.session import requires
from vigil.auth.roles import Permission
from vigil.config.paths import ResolvedPaths

TAIL_BYTES = 512 * 1024
MAX_LINES = 500
MAX_WALK_ENTRIES = 50_000
_LEVELS = ("debug", "info", "warning", "error", "critical")


class LogsOut(BaseModel):
    lines: list[dict[str, Any]]
    returned: int
    file_bytes: int | None  # None when there is no log file yet
    truncated_to_tail: bool


class StorageOut(BaseModel):
    disk_total_bytes: int
    disk_free_bytes: int
    database_bytes: int
    evidence_bytes: int
    evidence_files: int
    evidence_truncated: bool  # the walk stopped at its ceiling, so the figures are lower bounds
    logs_bytes: int


def tail_lines(path: Path, *, lines: int, level: str | None) -> LogsOut:
    """The newest `lines` records, oldest first, optionally at or above a level."""
    if not path.is_file():
        return LogsOut(lines=[], returned=0, file_bytes=None, truncated_to_tail=False)
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - TAIL_BYTES))
        chunk = handle.read()
    text = chunk.decode("utf-8", errors="replace")
    raw = text.splitlines()
    if size > TAIL_BYTES:
        raw = raw[1:]  # the first line was cut in half
    floor = _LEVELS.index(level) if level in _LEVELS else 0
    records: list[dict[str, Any]] = []
    for line in raw:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            item = {"event": line[:500], "level": "info", "unparsed": True}
        if not isinstance(item, dict):
            continue
        seen = str(item.get("level", "info")).lower()
        if seen in _LEVELS and _LEVELS.index(seen) < floor:
            continue
        records.append(item)
    chosen = records[-lines:]
    return LogsOut(
        lines=chosen, returned=len(chosen), file_bytes=size, truncated_to_tail=size > TAIL_BYTES
    )


def _folder_bytes(root: Path) -> tuple[int, int, bool]:
    total = count = 0
    for _dir, _subdirs, names in os.walk(root):
        for name in names:
            count += 1
            if count > MAX_WALK_ENTRIES:
                return total, count - 1, True
            try:
                total += (Path(_dir) / name).stat().st_size
            except OSError:
                continue  # a file removed mid-walk is not an error worth failing the page for
    return total, count, False


def storage_of(paths: ResolvedPaths) -> StorageOut:
    usage = shutil.disk_usage(paths.data_dir)
    database = sum(p.stat().st_size for p in paths.db_dir.glob("*") if p.is_file())
    evidence, files, truncated = _folder_bytes(paths.evidence_dir)
    logs = sum(p.stat().st_size for p in paths.log_file.parent.glob("*") if p.is_file())
    return StorageOut(
        disk_total_bytes=usage.total,
        disk_free_bytes=usage.free,
        database_bytes=database,
        evidence_bytes=evidence,
        evidence_files=files,
        evidence_truncated=truncated,
        logs_bytes=logs,
    )


def build_router(*, paths: ResolvedPaths) -> APIRouter:
    router = APIRouter(prefix="/system", tags=["system"])

    @router.get("/logs", response_model=LogsOut, dependencies=[requires(Permission.DIAGNOSTICS)])
    def logs(
        lines: Annotated[int, Query(ge=1, le=MAX_LINES)] = 200,
        level: Annotated[str | None, Query(pattern="^(debug|info|warning|error|critical)$")] = None,
    ) -> LogsOut:
        """Recent engine log records. Redaction happens where they are written, not here."""
        return tail_lines(paths.log_file, lines=lines, level=level)

    @router.get("/storage", response_model=StorageOut, dependencies=[requires(Permission.VIEW)])
    def storage() -> StorageOut:
        return storage_of(paths)

    return router
