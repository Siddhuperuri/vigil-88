"""Evidence records (05 §4, §6). Capture arrives in P4."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath

from vigil.core.ids import is_valid_ulid
from vigil.domain._validate import require_non_empty, require_non_negative, require_utc
from vigil.domain.enums import EvidenceKind

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    incident_id: str
    kind: EvidenceKind
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str
    captured_wall_utc: datetime
    span_ms: tuple[float, float] | None
    expires_wall_utc: datetime | None

    def __post_init__(self) -> None:
        if not is_valid_ulid(self.evidence_id) or not is_valid_ulid(self.incident_id):
            raise ValueError("evidence_id and incident_id must be ULIDs")
        require_non_empty("relative_path", self.relative_path)
        posix, win = PurePosixPath(self.relative_path), PureWindowsPath(self.relative_path)
        if posix.is_absolute() or win.is_absolute() or win.drive or ".." in posix.parts:
            raise ValueError("Evidence.relative_path must be relative with no '..' (01 §10)")
        if not _SHA256_RE.match(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        require_non_negative("size_bytes", self.size_bytes)
        require_utc("captured_wall_utc", self.captured_wall_utc)
        if self.expires_wall_utc is not None:
            require_utc("expires_wall_utc", self.expires_wall_utc)
        if self.span_ms is not None and self.span_ms[1] < self.span_ms[0]:
            raise ValueError("span_ms end precedes start")
