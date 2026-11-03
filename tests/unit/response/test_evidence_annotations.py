"""The manifest carries annotations about continuity, and reading them back trusts nothing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vigil.config.schema.retention import RetentionConfig
from vigil.core.clock import ManualClock
from vigil.core.ids import UlidFactory
from vigil.core.rng import seeded_rng
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.response.evidence import EvidenceWriter, IncidentEvidence, read_annotations

INCIDENT = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def session(tmp_path: Path) -> IncidentEvidence:
    writer = EvidenceWriter(
        evidence_root=tmp_path / "evidence",
        retention=RetentionConfig(),
        clock=ManualClock(),
        ulids=UlidFactory(ManualClock(), seeded_rng(5, "evidence")),
        logger=get_logger("test.evidence"),
        metrics=MetricsRegistry(),
    )
    opened = writer.open(
        camera_id="gate-north",
        incident_id=INCIDENT,
        confirmed_wall_utc=datetime(2026, 3, 1, 2, 41, tzinfo=UTC),
    )
    assert opened is not None
    return opened


def test_annotations_are_written_to_the_manifest_and_read_back(tmp_path: Path) -> None:
    s = session(tmp_path)
    s.annotate("stream_interrupted", True)
    s.annotate("gaps", [{"kind": "frame_gap", "start_ms": -2000.0, "end_ms": -500.0}])
    s.annotate("preroll_shortfall_ms", 1500.0)
    s.finish()
    found = read_annotations(s.directory / "manifest.json")
    assert found == {
        "stream_interrupted": True,
        "gaps": [{"kind": "frame_gap", "start_ms": -2000.0, "end_ms": -500.0}],
        "preroll_shortfall_ms": 1500.0,
    }


def test_a_manifest_without_annotations_reads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"files": []}), encoding="utf-8")
    assert read_annotations(path) is None


@pytest.mark.parametrize("text", ["{not json", "[]", '"x"', '{"annotations": 5}'])
def test_an_unreadable_manifest_is_none_not_an_error(tmp_path: Path, text: str) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(text, encoding="utf-8")
    assert read_annotations(path) is None


def test_a_missing_manifest_is_none(tmp_path: Path) -> None:
    assert read_annotations(tmp_path / "absent.json") is None


def test_hostile_entries_are_dropped_and_unknown_keys_ignored(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    body = {
        "annotations": {
            "gaps": [{"kind": "x"}, "nope", {"kind": "frame_gap", "start_ms": 1, "end_ms": 2}],
            "preroll_shortfall_ms": "lots",
            "secret": "ignored",
        }
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    assert read_annotations(path) == {
        "stream_interrupted": False,
        "gaps": [{"kind": "frame_gap", "start_ms": 1.0, "end_ms": 2.0}],
        "preroll_shortfall_ms": None,
    }
