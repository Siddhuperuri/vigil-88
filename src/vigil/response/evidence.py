"""Writing an incident's evidence (05 §6, 01 §6).

This module knows about **bytes, paths, hashes and failures**. It does not know about pixels,
cv2 or video codecs: the caller in `pipeline/` renders and encodes, because that is where the
imaging dependencies live, and hands the result here. The split is what lets every failure path
below be tested without a camera or an image library.

Four properties it is responsible for:

  1. **Never fabricate.** Missing pre-roll, a camera that disconnected mid-capture, an encoder
     that could not start -- each is recorded as a named failure and the incident is marked
     `partial`. There is no placeholder file and no zero-length stand-in.
  2. **Never half-write.** Every file lands via a `.part` temp file and one `os.replace`, which
     is atomic on both Windows and POSIX. A crash mid-write leaves a `.part` orphan for the
     retention sweep, never a truncated file that hashes to something nobody can reproduce.
  3. **Never escape the evidence directory.** Every path goes through `response/paths.py`.
  4. **Self-describing.** `manifest.json` lists every file with its kind, size, sha256 and span,
     so an evidence directory copied out of the system still explains itself -- which is exactly
     what happens when evidence is handed to someone.

The engine fails **open** (01 §6): a storage failure must not stop the incident being recorded,
so every method here reports failure rather than raising, and `finish()` says how complete the
result is.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from vigil.config.schema.retention import RetentionConfig
from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.core.ids import UlidFactory
from vigil.core.protocols.logger import LoggerLike
from vigil.domain.enums import EvidenceKind
from vigil.domain.evidence import Evidence
from vigil.observability.metrics import MetricsRegistry
from vigil.response.paths import EVIDENCE_MANIFEST, incident_dir, incident_relative_dir

PART_SUFFIX = ".part"
JSON_MEDIA_TYPE = "application/json"
JPEG_MEDIA_TYPE = "image/jpeg"
MP4_MEDIA_TYPE = "video/mp4"
# Refuse to start writing when the disk is this close to full. Filling a volume with evidence
# would take the database down with it, and of the two the database matters more.
MIN_FREE_BYTES = 64 * 1024 * 1024

# Which retention clock each kind runs on (05 §8). The JSON dumps are small and are part of the
# explanation, so they live as long as the incident; pixels and video expire sooner.
_RETENTION_FIELD: dict[EvidenceKind, str] = {
    EvidenceKind.SNAPSHOT_RAW: "snapshot_days",
    EvidenceKind.SNAPSHOT_ANNOTATED: "snapshot_days",
    EvidenceKind.CLIP_PRE: "clip_days",
    EvidenceKind.CLIP_POST: "clip_days",
    EvidenceKind.TRACK_DUMP: "incident_days",
    EvidenceKind.SCENE_DUMP: "incident_days",
    EvidenceKind.EXPLANATION: "incident_days",
}


class EvidenceStatus(StrEnum):
    """How complete an incident's evidence is. Persisted on the incident and shown in the UI.

    PENDING      capture has been requested and has not finished
    COMPLETE     every requested item was written and hashed
    PARTIAL      some items were written; `failures` says what was not, and why
    UNAVAILABLE  nothing could be written at all
    DISABLED     evidence capture is switched off in configuration
    """

    PENDING = "pending"
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class EvidenceFailure:
    kind: EvidenceKind
    filename: str
    reason: str


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    incident_id: str
    relative_dir: str
    records: tuple[Evidence, ...]
    failures: tuple[EvidenceFailure, ...]
    status: EvidenceStatus

    @property
    def bytes_written(self) -> int:
        return sum(r.size_bytes for r in self.records)

    @property
    def kinds(self) -> tuple[EvidenceKind, ...]:
        return tuple(r.kind for r in self.records)


@dataclass(slots=True)
class _Pending:
    records: list[Evidence] = field(default_factory=list)
    failures: list[EvidenceFailure] = field(default_factory=list)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk_bytes: int = 1024 * 1024) -> str:
    """Streamed, so hashing a 50 MB clip does not load it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, expected_sha256: str) -> bool:
    """Evidence whose hash does not match is served with a warning, never silently (05 §6)."""
    try:
        return sha256_file(path) == expected_sha256
    except OSError:
        return False


class IncidentEvidence:
    """One incident's capture session. Created by `EvidenceWriter.open`, closed by `finish`."""

    __slots__ = (
        "_clock",
        "_directory",
        "_log",
        "_metrics",
        "_pending",
        "_relative",
        "_retention",
        "_ulids",
        "incident_id",
    )

    def __init__(
        self,
        *,
        incident_id: str,
        directory: Path,
        relative_dir: str,
        retention: RetentionConfig,
        clock: Clock,
        ulids: UlidFactory,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self.incident_id = incident_id
        self._directory = directory
        self._relative = relative_dir
        self._retention = retention
        self._clock = clock
        self._ulids = ulids
        self._log = logger.bind(incident_id=incident_id)
        self._metrics = metrics
        self._pending = _Pending()

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def relative_dir(self) -> str:
        return self._relative

    # ---------------------------------------------------------------- writing

    def write(
        self,
        kind: EvidenceKind,
        filename: str,
        data: bytes,
        *,
        media_type: str,
        span_ms: tuple[float, float] | None = None,
    ) -> Evidence | None:
        """Write one file atomically and record it. None (plus a failure) on any storage error."""
        if not data:
            self.fail(kind, filename, "nothing to write: the encoder produced no bytes")
            return None
        target = self._directory / filename
        temp = target.with_name(target.name + PART_SUFFIX)
        try:
            self._ensure_space(len(data))
            with temp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(target)  # atomic on Windows and POSIX alike
        except (OSError, PersistenceError) as exc:
            self._cleanup(temp)
            self.fail(kind, filename, f"{type(exc).__name__}: {exc}")
            return None
        return self._record(kind, filename, sha256_bytes(data), len(data), media_type, span_ms)

    def write_json(self, kind: EvidenceKind, filename: str, payload: object) -> Evidence | None:
        try:
            data = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self.fail(kind, filename, f"cannot serialise: {exc}")
            return None
        return self.write(kind, filename, data, media_type=JSON_MEDIA_TYPE)

    def adopt(
        self,
        kind: EvidenceKind,
        filename: str,
        *,
        media_type: str,
        span_ms: tuple[float, float] | None = None,
    ) -> Evidence | None:
        """Record a file an encoder wrote into this directory in place (a muxed clip).

        It is hashed from disk rather than from memory, so what is recorded is what a reviewer
        will actually read back -- including if the encoder wrote less than it promised.
        """
        target = self._directory / filename
        try:
            size = target.stat().st_size
            if size == 0:
                self._cleanup(target)
                self.fail(kind, filename, "the encoder produced an empty file")
                return None
            digest = sha256_file(target)
        except OSError as exc:
            self.fail(kind, filename, f"{type(exc).__name__}: {exc}")
            return None
        return self._record(kind, filename, digest, size, media_type, span_ms)

    def fail(self, kind: EvidenceKind, filename: str, reason: str) -> None:
        """Record that an item could not be captured. Counted and logged, never swallowed."""
        self._pending.failures.append(EvidenceFailure(kind, filename, reason))
        self._metrics.counter("vigil_evidence_failures_total", kind=kind.value).inc()
        self._log.warning("evidence_item_failed", kind=kind.value, file=filename, reason=reason)

    def _record(
        self,
        kind: EvidenceKind,
        filename: str,
        digest: str,
        size: int,
        media_type: str,
        span_ms: tuple[float, float] | None,
    ) -> Evidence:
        record = Evidence(
            evidence_id=self._ulids.new(),
            incident_id=self.incident_id,
            kind=kind,
            relative_path=f"{self._relative}/{filename}",
            sha256=digest,
            size_bytes=size,
            media_type=media_type,
            captured_wall_utc=self._clock.wall_utc(),
            span_ms=span_ms,
            expires_wall_utc=self._expiry_for(kind),
        )
        self._pending.records.append(record)
        self._metrics.counter("vigil_evidence_written_total", kind=kind.value).inc()
        self._metrics.counter("vigil_evidence_bytes_total").inc(size)
        return record

    def _expiry_for(self, kind: EvidenceKind) -> datetime:
        days = int(getattr(self._retention, _RETENTION_FIELD[kind]))
        return self._clock.wall_utc() + timedelta(days=days)

    def _ensure_space(self, needed: int) -> None:
        usage = shutil.disk_usage(self._directory)
        if usage.free - needed < MIN_FREE_BYTES:
            raise PersistenceError(
                "refusing to write evidence: the volume is nearly full",
                context={"free_bytes": usage.free, "needed_bytes": needed},
            )

    def _cleanup(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self._log.warning("evidence_temp_cleanup_failed", file=path.name, error=str(exc))

    # ---------------------------------------------------------------- finishing

    def finish(self, *, requested: int | None = None) -> EvidenceReport:
        """Write the manifest and report how complete the capture was."""
        records = tuple(self._pending.records)
        failures = tuple(self._pending.failures)
        status = self._status(records, failures, requested)
        self._write_manifest(records, failures, status)
        return EvidenceReport(
            incident_id=self.incident_id,
            relative_dir=self._relative,
            records=records,
            failures=failures,
            status=status,
        )

    @staticmethod
    def _status(
        records: Sequence[Evidence],
        failures: Sequence[EvidenceFailure],
        requested: int | None,
    ) -> EvidenceStatus:
        if not records:
            return EvidenceStatus.UNAVAILABLE
        if failures:
            return EvidenceStatus.PARTIAL
        if requested is not None and len(records) < requested:
            return EvidenceStatus.PARTIAL
        return EvidenceStatus.COMPLETE

    def _write_manifest(
        self,
        records: Sequence[Evidence],
        failures: Sequence[EvidenceFailure],
        status: EvidenceStatus,
    ) -> None:
        manifest = {
            "incident_id": self.incident_id,
            "status": status.value,
            "written_wall_utc": self._clock.wall_utc().isoformat(),
            "files": [
                {
                    "kind": r.kind.value,
                    "file": r.relative_path.rsplit("/", 1)[-1],
                    "media_type": r.media_type,
                    "bytes": r.size_bytes,
                    "sha256": r.sha256,
                    "captured_wall_utc": r.captured_wall_utc.isoformat(),
                    "span_ms": list(r.span_ms) if r.span_ms else None,
                    "expires_wall_utc": (
                        r.expires_wall_utc.isoformat() if r.expires_wall_utc else None
                    ),
                }
                for r in records
            ],
            # Failures go in the manifest too. A directory that silently lacked a clip would
            # leave a reviewer wondering whether the system or the camera was at fault.
            "missing": [
                {"kind": f.kind.value, "file": f.filename, "reason": f.reason} for f in failures
            ],
        }
        target = self._directory / EVIDENCE_MANIFEST
        temp = target.with_name(target.name + PART_SUFFIX)
        try:
            temp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            temp.replace(target)  # atomic on Windows and POSIX alike
        except OSError as exc:
            self._cleanup(temp)
            self._log.error("evidence_manifest_failed", error=f"{type(exc).__name__}: {exc}")
            self._metrics.counter("vigil_evidence_failures_total", kind="manifest").inc()


class EvidenceWriter:
    """Opens a capture session per incident. One per process; holds no per-incident state."""

    __slots__ = ("_clock", "_enabled", "_log", "_metrics", "_retention", "_root", "_ulids")

    def __init__(
        self,
        *,
        evidence_root: Path,
        retention: RetentionConfig,
        clock: Clock,
        ulids: UlidFactory,
        logger: LoggerLike,
        metrics: MetricsRegistry,
        enabled: bool = True,
    ) -> None:
        self._root = evidence_root
        self._retention = retention
        self._clock = clock
        self._ulids = ulids
        self._log = logger.bind(component="evidence")
        self._metrics = metrics
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def root(self) -> Path:
        return self._root

    def relative_dir(self, camera_id: str, incident_id: str, confirmed_wall_utc: datetime) -> str:
        return incident_relative_dir(camera_id, incident_id, confirmed_wall_utc)

    def open(
        self, *, camera_id: str, incident_id: str, confirmed_wall_utc: datetime
    ) -> IncidentEvidence | None:
        """Create the incident's directory and return its session, or None if it cannot be made.

        Returning None rather than raising is the fail-open rule: the incident is still recorded,
        with evidence marked UNAVAILABLE and the reason logged.
        """
        if not self._enabled:
            return None
        try:
            directory = incident_dir(self._root, camera_id, incident_id, confirmed_wall_utc)
            directory.mkdir(parents=True, exist_ok=True)
        except (OSError, PersistenceError) as exc:
            self._metrics.counter("vigil_evidence_failures_total", kind="directory").inc()
            self._log.error(
                "evidence_directory_failed",
                incident_id=incident_id,
                camera_id=camera_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        return IncidentEvidence(
            incident_id=incident_id,
            directory=directory,
            relative_dir=self.relative_dir(camera_id, incident_id, confirmed_wall_utc),
            retention=self._retention,
            clock=self._clock,
            ulids=self._ulids,
            logger=self._log,
            metrics=self._metrics,
        )
