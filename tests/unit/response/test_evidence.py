"""Evidence capture: the failure paths, the path guard, and the promise never to fabricate.

The happy path is covered end to end in `tests/integration/test_incident_workflow.py`. What is
worth testing in isolation is everything that goes wrong -- a full disk, a read-only directory,
an encoder that produces nothing, a write interrupted mid-flight -- because those are the cases
where a system is tempted to write a placeholder and call it evidence.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vigil.config.schema.retention import RetentionConfig
from vigil.core.clock import ManualClock
from vigil.core.errors import PersistenceError
from vigil.core.ids import UlidFactory
from vigil.core.rng import seeded_rng
from vigil.domain.enums import EvidenceKind
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.response.evidence import (
    JPEG_MEDIA_TYPE,
    MIN_FREE_BYTES,
    PART_SUFFIX,
    EvidenceStatus,
    EvidenceWriter,
    IncidentEvidence,
    sha256_bytes,
    sha256_file,
    verify_file,
)
from vigil.response.paths import (
    incident_relative_dir,
    resolve_relative,
    safe_component,
    safe_join,
)

CAMERA = "gate-north"
INCIDENT = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
CONFIRMED = datetime(2026, 3, 1, 2, 41, tzinfo=UTC)
PAYLOAD = b"\xff\xd8\xff\xe0 not really a jpeg, but bytes are bytes"


@pytest.fixture
def writer(tmp_path: Path) -> EvidenceWriter:
    return EvidenceWriter(
        evidence_root=tmp_path / "evidence",
        retention=RetentionConfig(),
        clock=ManualClock(),
        ulids=UlidFactory(ManualClock(), seeded_rng(5, "evidence")),
        logger=get_logger("test.evidence"),
        metrics=MetricsRegistry(),
    )


@pytest.fixture
def session(writer: EvidenceWriter) -> IncidentEvidence:
    opened = writer.open(camera_id=CAMERA, incident_id=INCIDENT, confirmed_wall_utc=CONFIRMED)
    assert opened is not None
    return opened


# ---------------------------------------------------------------- paths


def test_the_layout_is_date_partitioned_under_the_camera() -> None:
    """So retention deletion is a directory walk with no database query (05 §6)."""
    assert incident_relative_dir(CAMERA, INCIDENT, CONFIRMED) == f"{CAMERA}/2026/03/01/{INCIDENT}"


@pytest.mark.parametrize(
    "component",
    ["..", "../etc", "a/b", "a\\b", "C:", ".hidden", "", "x" * 100, "with space"],
)
def test_an_unsafe_path_component_is_refused(component: str) -> None:
    with pytest.raises(PersistenceError, match="unsafe"):
        safe_component(component, label="test")


def test_safe_join_refuses_an_escape(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    assert safe_join(root, "cam", "2026") == (root / "cam" / "2026").resolve()
    with pytest.raises(PersistenceError, match="unsafe"):
        safe_join(root, "..", "elsewhere")


@pytest.mark.parametrize(
    "stored",
    [
        "../../../../etc/passwd",
        "/etc/passwd",
        "\\\\server\\share\\x",
        "cam/../../outside.txt",
        "",
    ],
)
def test_a_tampered_stored_path_cannot_become_an_arbitrary_file_read(
    tmp_path: Path, stored: str
) -> None:
    """A database row is still input: this is the check before any file is opened (01 §10)."""
    root = tmp_path / "evidence"
    root.mkdir()
    with pytest.raises(PersistenceError):
        resolve_relative(root, stored)


def test_a_legitimate_stored_path_resolves_under_the_root(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    resolved = resolve_relative(root, f"{CAMERA}/2026/03/01/{INCIDENT}/snapshot_raw.jpg")
    assert resolved.is_relative_to(root.resolve())


# ---------------------------------------------------------------- writing


def test_a_written_item_is_hashed_from_what_was_written(session: IncidentEvidence) -> None:
    record = session.write(
        EvidenceKind.SNAPSHOT_RAW, "snapshot_raw.jpg", PAYLOAD, media_type=JPEG_MEDIA_TYPE
    )
    assert record is not None
    assert record.sha256 == sha256_bytes(PAYLOAD)
    path = session.directory / "snapshot_raw.jpg"
    assert path.read_bytes() == PAYLOAD
    assert sha256_file(path) == record.sha256
    assert verify_file(path, record.sha256)
    assert record.relative_path == f"{CAMERA}/2026/03/01/{INCIDENT}/snapshot_raw.jpg"


def test_nothing_is_left_half_written(session: IncidentEvidence) -> None:
    """Every write lands via one atomic replace, so no `.part` file survives a success."""
    session.write(EvidenceKind.SNAPSHOT_RAW, "snapshot_raw.jpg", PAYLOAD, media_type="image/jpeg")
    assert not list(session.directory.glob(f"*{PART_SUFFIX}"))


def test_an_empty_payload_is_a_failure_not_a_zero_byte_file(
    session: IncidentEvidence,
) -> None:
    assert session.write(EvidenceKind.CLIP_PRE, "clip_pre.mp4", b"", media_type="video/mp4") is None
    report = session.finish()
    assert report.status is EvidenceStatus.UNAVAILABLE
    assert "no bytes" in report.failures[0].reason
    assert not (session.directory / "clip_pre.mp4").exists()


def test_an_unserialisable_payload_is_reported_not_raised(session: IncidentEvidence) -> None:
    class Opaque:
        __slots__ = ()

    # `default=str` handles most things; a key that is not a string is what json refuses.
    assert session.write_json(EvidenceKind.SCENE_DUMP, "scene.json", {Opaque(): 1}) is None
    assert session.finish().failures


def test_a_full_disk_is_refused_before_anything_is_written(
    session: IncidentEvidence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filling a volume with evidence would take the database down with it."""
    import shutil

    class Usage:
        total = 100 * MIN_FREE_BYTES
        used = total
        free = 1024

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage())
    assert (
        session.write(
            EvidenceKind.SNAPSHOT_RAW, "snapshot_raw.jpg", PAYLOAD, media_type="image/jpeg"
        )
        is None
    )
    report = session.finish()
    assert report.status is EvidenceStatus.UNAVAILABLE
    assert "nearly full" in report.failures[0].reason


def test_a_write_failure_is_recorded_and_the_rest_still_lands(
    session: IncidentEvidence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail open (01 §6): one bad item must not cost the whole bundle."""
    real_replace = Path.replace

    def refuse(self: Path, target: object) -> None:
        if "snapshot" in self.name:
            raise OSError(13, "permission denied")
        real_replace(self, target)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "replace", refuse)
    assert (
        session.write(
            EvidenceKind.SNAPSHOT_RAW, "snapshot_raw.jpg", PAYLOAD, media_type="image/jpeg"
        )
        is None
    )
    assert (
        session.write_json(EvidenceKind.EXPLANATION, "explanation.json", {"ok": True}) is not None
    )

    report = session.finish()
    assert report.status is EvidenceStatus.PARTIAL
    assert [f.filename for f in report.failures] == ["snapshot_raw.jpg"]
    assert "permission denied" in report.failures[0].reason
    assert [r.kind for r in report.records] == [EvidenceKind.EXPLANATION]
    # The temp file does not survive a failed write.
    assert not list(session.directory.glob(f"*{PART_SUFFIX}"))


def test_adopting_an_empty_file_is_a_failure(session: IncidentEvidence) -> None:
    """An encoder that opened a file and wrote nothing has not produced a clip."""
    (session.directory / "clip_pre.mp4").write_bytes(b"")
    assert session.adopt(EvidenceKind.CLIP_PRE, "clip_pre.mp4", media_type="video/mp4") is None
    assert not (session.directory / "clip_pre.mp4").exists()
    assert "empty file" in session.finish().failures[0].reason


def test_adopting_hashes_from_disk_not_from_a_promise(session: IncidentEvidence) -> None:
    (session.directory / "clip_pre.mp4").write_bytes(PAYLOAD)
    record = session.adopt(
        EvidenceKind.CLIP_PRE, "clip_pre.mp4", media_type="video/mp4", span_ms=(0.0, 5000.0)
    )
    assert record is not None
    assert record.sha256 == sha256_bytes(PAYLOAD)
    assert record.span_ms == (0.0, 5000.0)


def test_a_missing_file_adopts_as_a_failure(session: IncidentEvidence) -> None:
    assert session.adopt(EvidenceKind.CLIP_POST, "clip_post.mp4", media_type="video/mp4") is None
    assert session.finish().failures


# ---------------------------------------------------------------- the manifest


def test_the_manifest_lists_what_is_there_and_what_is_not(session: IncidentEvidence) -> None:
    """An evidence directory copied out of the system still explains itself (05 §6)."""
    session.write(EvidenceKind.SNAPSHOT_RAW, "snapshot_raw.jpg", PAYLOAD, media_type="image/jpeg")
    session.fail(EvidenceKind.CLIP_PRE, "clip_pre.mp4", "this build has no mp4 encoder")
    report = session.finish()

    manifest = json.loads((session.directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["incident_id"] == INCIDENT
    assert manifest["status"] == EvidenceStatus.PARTIAL.value
    assert [f["kind"] for f in manifest["files"]] == ["snapshot_raw"]
    assert manifest["files"][0]["sha256"] == sha256_bytes(PAYLOAD)
    # A directory that silently lacked a clip would leave a reviewer guessing.
    assert manifest["missing"] == [
        {"kind": "clip_pre", "file": "clip_pre.mp4", "reason": "this build has no mp4 encoder"}
    ]
    assert report.status is EvidenceStatus.PARTIAL


def test_expiry_follows_the_kind_not_one_global_clock(session: IncidentEvidence) -> None:
    """Pixels and video expire sooner than the explanation they belong to (05 §8)."""
    snapshot = session.write(
        EvidenceKind.SNAPSHOT_RAW, "snapshot_raw.jpg", PAYLOAD, media_type="image/jpeg"
    )
    explanation = session.write_json(EvidenceKind.EXPLANATION, "explanation.json", {"a": 1})
    assert snapshot is not None and explanation is not None
    assert snapshot.expires_wall_utc is not None
    assert explanation.expires_wall_utc is not None
    assert explanation.expires_wall_utc > snapshot.expires_wall_utc


def test_a_complete_bundle_reports_complete(session: IncidentEvidence) -> None:
    session.write(EvidenceKind.SNAPSHOT_RAW, "snapshot_raw.jpg", PAYLOAD, media_type="image/jpeg")
    session.write_json(EvidenceKind.EXPLANATION, "explanation.json", {"a": 1})
    report = session.finish(requested=2)
    assert report.status is EvidenceStatus.COMPLETE
    assert report.bytes_written > 0
    assert set(report.kinds) == {EvidenceKind.SNAPSHOT_RAW, EvidenceKind.EXPLANATION}


def test_fewer_items_than_requested_is_partial(session: IncidentEvidence) -> None:
    """Even with no individual failure: a thinner bundle than asked for is not complete."""
    session.write_json(EvidenceKind.EXPLANATION, "explanation.json", {"a": 1})
    assert session.finish(requested=5).status is EvidenceStatus.PARTIAL


# ---------------------------------------------------------------- the writer


def test_evidence_disabled_opens_nothing(tmp_path: Path) -> None:
    writer = EvidenceWriter(
        evidence_root=tmp_path / "evidence",
        retention=RetentionConfig(),
        clock=ManualClock(),
        ulids=UlidFactory(ManualClock(), seeded_rng(1, "x")),
        logger=get_logger("t"),
        metrics=MetricsRegistry(),
        enabled=False,
    )
    assert not writer.enabled
    assert writer.open(camera_id=CAMERA, incident_id=INCIDENT, confirmed_wall_utc=CONFIRMED) is None


def test_a_directory_that_cannot_be_created_returns_none_rather_than_raising(
    writer: EvidenceWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail open: the incident is still recorded, with evidence marked unavailable."""
    monkeypatch.setattr(
        Path, "mkdir", lambda *_a, **_k: (_ for _ in ()).throw(OSError(13, "denied"))
    )
    assert writer.open(camera_id=CAMERA, incident_id=INCIDENT, confirmed_wall_utc=CONFIRMED) is None


def test_an_unsafe_camera_id_cannot_open_a_session(writer: EvidenceWriter) -> None:
    assert (
        writer.open(camera_id="../../etc", incident_id=INCIDENT, confirmed_wall_utc=CONFIRMED)
        is None
    )


def test_verify_file_is_false_for_a_missing_file(tmp_path: Path) -> None:
    assert not verify_file(tmp_path / "nope.jpg", "0" * 64)


def test_an_interrupted_write_leaves_only_a_part_file(session: IncidentEvidence) -> None:
    """What a crash mid-write looks like: a `.part` orphan the retention sweep collects, never
    a truncated file that hashes to something nobody can reproduce."""
    orphan = session.directory / f"clip_pre.mp4{PART_SUFFIX}"
    orphan.write_bytes(b"half a clip")
    report = session.finish()
    assert report.records == ()
    assert orphan.exists(), "the sweep collects it later; finish() does not"
    assert not (session.directory / "clip_pre.mp4").exists()


def test_the_manifest_survives_a_failure_to_write_it(
    session: IncidentEvidence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that cannot be written is counted and logged; the report is still returned."""
    session.write_json(EvidenceKind.EXPLANATION, "explanation.json", {"a": 1})
    monkeypatch.setattr(
        Path, "write_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError(28, "no space"))
    )
    report = session.finish()
    assert report.records, "the items that landed are still reported"
    assert not (session.directory / "manifest.json").exists()


def test_os_fsync_is_called_so_a_power_cut_cannot_lose_a_written_item(
    session: IncidentEvidence, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real_fsync(fd))
    session.write(EvidenceKind.SNAPSHOT_RAW, "snapshot_raw.jpg", PAYLOAD, media_type="image/jpeg")
    assert calls, "evidence is flushed to the device, not just to the page cache"
