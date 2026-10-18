"""The PDF report: it is a real PDF, it says what the other formats say, and it is honest.

Two kinds of check here. The first is structural — a byte stream that claims to be a PDF has a
header, a trailer and a cross-reference table, and a reader that cannot find them shows nothing.
The second is content: the text that must be in the document, and the text that must **not** be,
read back out of the page streams rather than assumed from the code that wrote them.

The renderer comes from the optional `reports` extra. It is in the dev group so the gate always
exercises this path, and the unavailable path is driven by patching the probe rather than by
uninstalling anything.
"""

from __future__ import annotations

import base64
import io
import re
import zlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.analytics.test_export import WALL, WINDOW, _report, store  # noqa: F401
from vigil.analytics import pdf
from vigil.analytics.export import (
    ALWAYS_AVAILABLE,
    ExportFormat,
    export_path,
    format_unavailable_reason,
    write_report,
)
from vigil.analytics.reports import Report
from vigil.core.errors import PersistenceError
from vigil.domain.enums import IncidentStatus
from vigil.persistence.database import Database

PDF_HEADER = b"%PDF-"
PDF_TRAILER = b"%%EOF"


def _render(report: Report) -> bytes:
    buffer = io.BytesIO()
    pdf.write_pdf(report, buffer)
    return buffer.getvalue()


def _inflate(raw: bytes) -> bytes:
    """Content streams here are ASCII85 then Flate, which is reportlab's default pair."""
    body = raw.strip()
    try:
        return zlib.decompress(base64.a85decode(body, adobe=True))
    except (ValueError, zlib.error):
        pass
    try:
        return zlib.decompress(body)
    except zlib.error:
        return body


def _text(document: bytes) -> str:
    """Pull the drawable text out of every content stream.

    Deliberately crude: it extracts the string operands of `Tj` from each stream, which is
    enough to assert that a phrase reached the page. A full parser here would be testing
    `reportlab`, not this module.
    """
    chunks: list[str] = []
    for raw in re.findall(rb"stream\n(.*?)endstream", document, re.S):
        for literal in re.findall(rb"\(((?:[^()\\]|\\.)*)\)\s*Tj", _inflate(raw)):
            chunks.append(literal.replace(rb"\(", b"(").replace(rb"\)", b")").decode("latin-1"))
    return "\n".join(chunks)


# ---------------------------------------------------------------- availability


def test_the_renderer_is_installed_for_the_gate() -> None:
    """If this fails the dev group lost `reportlab` and every other test here is vacuous."""
    assert pdf.is_available() is True
    assert pdf.unavailable_reason() is None


def test_json_and_csv_never_report_themselves_unavailable() -> None:
    for candidate in ALWAYS_AVAILABLE:
        assert format_unavailable_reason(candidate) is None


def test_an_absent_renderer_gives_an_actionable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative is not enough: the answer has to say what would fix it (D-008)."""
    monkeypatch.setattr(pdf, "is_available", lambda: False)
    reason = pdf.unavailable_reason()
    assert reason is not None
    assert pdf.INSTALL_HINT in reason
    assert "JSON and CSV" in reason
    assert format_unavailable_reason(ExportFormat.PDF) == reason


# ---------------------------------------------------------------- the document


def test_the_output_is_a_structurally_complete_pdf(store: Database) -> None:  # noqa: F811
    document = _render(_report(store))
    assert document.startswith(PDF_HEADER)
    assert document.rstrip().endswith(PDF_TRAILER)
    assert b"xref" in document
    assert b"/Type /Catalog" in document or b"/Type/Catalog" in document


def test_the_document_carries_the_window_and_the_generation_time(
    store: Database,  # noqa: F811
) -> None:
    text = _text(_render(_report(store)))
    assert "VIGIL-88 incident report" in text
    assert WINDOW.since.isoformat() in text
    assert "closed-open" in text


def test_an_incident_appears_with_its_reference_and_trigger(store: Database) -> None:  # noqa: F811
    report = _report(store, count=2)
    text = _text(_render(report))
    for row in report.incidents:
        assert row.incident.display_ref in text
        assert row.incident.severity.name in text
        assert row.incident.camera_id in text
    assert report.incidents[0].incident.explanation.trigger_summary[:24] in text


def test_the_operator_note_is_reproduced(store: Database) -> None:  # noqa: F811
    text = _text(_render(_report(store, note="checked on camera, nobody there")))
    assert "checked on camera" in text


def test_the_timeline_is_rendered_and_the_first_transition_says_new(
    store: Database,  # noqa: F811
) -> None:
    report = _report(store)
    assert report.incidents[0].incident.transitions
    text = _text(_render(report))
    assert "timeline" in text
    assert "(new) -> candidate" in text
    assert "-> confirmed" in text


def test_an_unavailable_metric_is_printed_as_unavailable_not_as_zero(
    store: Database,  # noqa: F811
) -> None:
    """The single most important line in the file. A blank or a 0 in a column of numbers reads
    as a measurement; nothing acknowledged means there is no acknowledgement time."""
    report = _report(store)
    assert report.summary.acknowledgement.available is False
    text = _text(_render(report))
    assert "time to acknowledge" in text
    assert "unavailable" in text


def test_the_dismissal_rate_is_not_called_a_false_positive_rate(
    store: Database,  # noqa: F811
) -> None:
    text = _text(_render(_report(store)))
    assert "not the detector's false-positive rate" in text


def test_the_caveats_are_reproduced_verbatim(store: Database) -> None:  # noqa: F811
    report = _report(store)
    assert report.caveats
    text = _text(_render(report))
    for caveat in report.caveats:
        # Wrapped across lines, so compare on a prefix that fits one.
        assert caveat[:40] in text.replace("\n", "")


def test_evidence_is_referenced_and_never_embedded(store: Database) -> None:  # noqa: F811
    """A frame inside a report is a copy of evidence with no hash to check it against."""
    document = _render(_report(store))
    assert b"/Subtype /Image" not in document
    assert b"/DCTDecode" not in document
    assert "evidence" in _text(document)


def test_an_empty_report_is_a_document_that_says_so(store: Database) -> None:  # noqa: F811
    from vigil.analytics.queries import AnalyticsQueries
    from vigil.analytics.reports import ReportBuilder
    from vigil.persistence.repositories.evidence import EvidenceRepository
    from vigil.persistence.repositories.incidents import IncidentRepository

    empty = ReportBuilder(
        analytics=AnalyticsQueries(store),
        incidents=IncidentRepository(store),
        evidence=EvidenceRepository(store),
    ).build(WINDOW, generated_wall_utc=WALL)
    text = _text(_render(empty))
    assert "no incidents matched this window and filter" in text
    assert "Incidents (0)" in text


def test_metric_definitions_travel_with_the_document(store: Database) -> None:  # noqa: F811
    text = _text(_render(_report(store)))
    assert "Metric definitions" in text
    assert "dismissal_rate" in text


def test_a_long_note_is_wrapped_rather_than_running_off_the_page(
    store: Database,  # noqa: F811
) -> None:
    long_note = "x" * 600
    text = _text(_render(_report(store, note=long_note)))
    # The runs of the note itself, rather than any line containing an 'x' -- "max" appears in
    # the metric definitions.
    runs = re.findall(r"x{20,}", text)
    # Several pieces, none wider than the page, and every character accounted for: wrapped, not
    # truncated and not overflowing into the margin.
    assert len(runs) > 1
    assert all(len(run) <= pdf.WRAP_COLUMNS for run in runs)
    assert sum(len(run) for run in runs) == len(long_note)


def test_many_incidents_run_onto_further_pages(store: Database) -> None:  # noqa: F811
    document = _render(_report(store, count=14))
    assert document.count(b"/Type /Page\n") > 1 or document.count(b"/Type /Page") > 2


# ---------------------------------------------------------------- written to a file


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    folder = tmp_path / "exports"
    folder.mkdir()
    return folder


def test_a_pdf_export_lands_inside_the_exports_directory(
    store: Database,  # noqa: F811
    exports: Path,
) -> None:
    result = write_report(
        _report(store),
        exports_dir=exports,
        filename="march.pdf",
        export_format=ExportFormat.PDF,
    )
    assert result.path.name == "march.pdf"
    assert result.path.resolve().is_relative_to(exports.resolve())
    assert result.rows == 1
    assert result.bytes_written > 0
    assert result.path.read_bytes().startswith(PDF_HEADER)


def test_no_part_file_survives_a_pdf_export(
    store: Database,  # noqa: F811
    exports: Path,
) -> None:
    write_report(
        _report(store),
        exports_dir=exports,
        filename="march.pdf",
        export_format=ExportFormat.PDF,
    )
    assert not list(exports.glob("*.part"))


def test_a_pdf_suffix_is_accepted_and_an_unknown_one_is_not(exports: Path) -> None:
    assert export_path(exports, "x.pdf").name == "x.pdf"
    with pytest.raises(PersistenceError, match=r"\.csv, \.json, \.pdf"):
        export_path(exports, "x.docx")


@pytest.mark.parametrize("name", ["../escape.pdf", "sub/dir.pdf", "C:/evil.pdf"])
def test_a_pdf_export_cannot_be_written_outside_the_directory(exports: Path, name: str) -> None:
    with pytest.raises(PersistenceError):
        export_path(exports, name)


def test_writing_a_pdf_without_the_renderer_is_refused_before_the_file_is_made(
    store: Database,  # noqa: F811
    exports: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No empty file, no `.part` left behind: the refusal happens before anything is opened."""
    monkeypatch.setattr(pdf, "is_available", lambda: False)
    with pytest.raises(PersistenceError, match="cannot export as pdf"):
        write_report(
            _report(store),
            exports_dir=exports,
            filename="march.pdf",
            export_format=ExportFormat.PDF,
        )
    assert not list(exports.iterdir())


# ---------------------------------------------------------------- the honesty branches

# These paths cannot be produced by the seeding fixture (nothing is resolved, no camera health
# is recorded), and they are the ones where a wrong rendering would mislead most. The report is
# therefore rebuilt field by field from a real one, so the renderer still sees real objects.


def test_camera_availability_prints_unknown_for_a_camera_with_no_history(
    store: Database,  # noqa: F811
) -> None:
    report = _report(store)
    text = _text(
        _render(
            replace(
                report,
                summary=replace(
                    report.summary,
                    camera_availability={
                        "gate-north": {"online": 3600.0, "degraded": 400.0},
                        "yard-west": None,
                    },
                ),
            )
        )
    )
    assert "gate-north: 90.0% online" in text
    assert "yard-west: unknown (no transitions recorded)" in text
    assert "yard-west: 100.0% online" not in text


def test_camera_availability_says_so_when_nothing_was_recorded(
    store: Database,  # noqa: F811
) -> None:
    report = _report(store)
    text = _text(_render(replace(report, summary=replace(report.summary, camera_availability={}))))
    assert "(no health history recorded)" in text


def test_a_resolved_incident_shows_its_resolution_and_zones(
    store: Database,  # noqa: F811
) -> None:
    report = _report(store)
    row = report.incidents[0]
    resolved_at = row.incident.confirmed_wall_utc + timedelta(minutes=4)
    text = _text(
        _render(
            replace(
                report,
                incidents=(
                    replace(
                        row,
                        incident=replace(
                            row.incident,
                            status=IncidentStatus.RESOLVED,
                            resolved_wall_utc=resolved_at,
                            duration_ms=240_000.0,
                            zone_ids=frozenset({"yard", "gate"}),
                        ),
                    ),
                ),
            )
        )
    )
    assert f"resolved    {resolved_at.isoformat()}" in text
    assert "zones gate yard" in text


def test_an_incident_with_no_recorded_transitions_says_the_timeline_is_unavailable(
    store: Database,  # noqa: F811
) -> None:
    """Unavailable, not an empty list rendered as a complete history of nothing."""
    report = _report(store)
    row = report.incidents[0]
    text = _text(
        _render(
            replace(
                report,
                incidents=(replace(row, incident=replace(row.incident, transitions=())),),
            )
        )
    )
    assert "timeline unavailable (no transitions recorded for this incident)" in text
