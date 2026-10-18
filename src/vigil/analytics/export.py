"""Writing a report out: JSON and CSV (10-roadmap §8).

**Where a file may be written is a security question, not a convenience one.** An export
destination is the one place in this system where a caller gets to influence a filesystem path,
so it goes through the same guard evidence paths do: each component is pattern-checked and the
resolved path must still be inside the configured exports directory. A caller cannot name a
directory, cannot climb out of it with `..`, and cannot overwrite something outside it.

**CSV needs more than quoting.** `csv.writer` handles delimiters, quotes and newlines. It does
not handle the fact that a spreadsheet executes any cell beginning `=`, `+`, `-`, `@`, tab or
carriage return — so a camera named `=cmd|...` or an operator note beginning `=HYPERLINK(...)`
becomes a formula in the reader's spreadsheet, not text. Every cell is therefore neutralised by
prefixing a single quote when it starts with one of those characters. This is the injection
vector that actually gets exploited in exported security reports, and quoting alone does not
close it.

**Large exports stream.** Rows are written as they are produced and never collected into one
string, so the memory cost of an export is one row rather than the whole result.

**PDF is deliberately not implemented.** 10-roadmap §8 lists "CSV/JSON/PDF export", but every
route to a PDF means a new dependency, and `03 §1` requires each one to be justified by name in
that document — none is. JSON and CSV cover the stated use ("opens correctly outside the system
and contains evidence, explanation and timeline"), and the console's print stylesheet covers a
human-readable page. Recorded in `docs/LIMITATIONS.md` rather than half-built.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from vigil.analytics import pdf
from vigil.analytics.definitions import Count
from vigil.analytics.reports import IncidentRow, Report
from vigil.analytics.serialise import filter_json, write_json
from vigil.core.errors import PersistenceError
from vigil.response.paths import safe_join

# Characters a spreadsheet treats as the start of a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
CSV_NEWLINE = ""
MAX_CELL_LENGTH = 32_000  # a spreadsheet cell ceiling; longer values are truncated and marked


class ExportFormat(StrEnum):
    JSON = "json"
    CSV = "csv"
    # Needs the optional `reports` extra. The format is always *offered* and reports itself
    # unavailable with the install command when the renderer is absent, which is more useful
    # than hiding it: an operator who wants a PDF learns how to get one (ADR 0014 §C).
    PDF = "pdf"


#: Accepted export suffixes. A format not listed here cannot be written to a file at all,
#: which is a stronger statement than "the renderer is missing".
SUFFIXES = frozenset({".json", ".csv", ".pdf"})

#: Formats that need nothing beyond the core install.
ALWAYS_AVAILABLE = frozenset({ExportFormat.JSON, ExportFormat.CSV})


def format_unavailable_reason(export_format: ExportFormat) -> str | None:
    """Why this format cannot be produced here, or `None` when it can."""
    if export_format in ALWAYS_AVAILABLE:
        return None
    return pdf.unavailable_reason()


INCIDENT_COLUMNS = (
    "display_ref",
    "incident_id",
    "camera_id",
    "location",
    "event_type",
    "module_id",
    "module_version",
    "status",
    "severity",
    "severity_score",
    "confidence_ratio",
    "first_observed_utc",
    "confirmed_utc",
    "last_observed_utc",
    "resolved_utc",
    "detection_latency_ms",
    "duration_ms",
    "zone_ids",
    "involved_track_ids",
    "tracker_epoch",
    "operator_note",
    "evidence_status",
    "evidence_count",
    "evidence_ids",
    "evidence_paths",
    "trigger_summary",
    "model_name",
    "model_backend",
    "model_device",
    "config_hash",
)


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    format: ExportFormat
    rows: int
    bytes_written: int


def neutralise(value: str) -> str:
    """Make a cell inert in a spreadsheet without changing what it says.

    A leading apostrophe is how every spreadsheet marks "this is text"; it is stripped on
    display, so the reader sees the original value and the formula never runs.
    """
    if value and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text or "0"
    text = str(value)
    if len(text) > MAX_CELL_LENGTH:
        text = text[: MAX_CELL_LENGTH - 12] + "...TRUNCATED"
    return neutralise(text)


def export_path(exports_dir: Path, filename: str) -> Path:
    """Resolve an export destination, refusing anything that escapes `exports_dir`.

    The filename is a *name*, never a path: `safe_join` rejects separators, `..`, drive letters
    and leading dots, and then re-checks that the resolved path is still inside the root.
    """
    if not filename or filename != Path(filename).name:
        raise PersistenceError(f"an export filename must be a plain name, not a path: {filename!r}")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUFFIXES:
        allowed = ", ".join(sorted(SUFFIXES))
        raise PersistenceError(
            f"an export must be written as {allowed}, not {suffix or '(no suffix)'!r}"
        )
    return safe_join(exports_dir, filename)


# ---------------------------------------------------------------- CSV


def incident_cells(row: IncidentRow) -> list[str]:
    incident = row.incident
    model = incident.model_metadata
    values: dict[str, object] = {
        "display_ref": incident.display_ref,
        "incident_id": incident.incident_id,
        "camera_id": incident.camera_id,
        "location": incident.location,
        "event_type": incident.event_type.value,
        "module_id": incident.module_id,
        "module_version": incident.module_version,
        "status": incident.status.value,
        "severity": incident.severity.name,
        "severity_score": incident.severity_score,
        "confidence_ratio": incident.confidence_ratio,
        "first_observed_utc": incident.first_observed_wall_utc.isoformat(),
        "confirmed_utc": incident.confirmed_wall_utc.isoformat(),
        "last_observed_utc": incident.last_observed_wall_utc.isoformat(),
        "resolved_utc": (
            None if incident.resolved_wall_utc is None else incident.resolved_wall_utc.isoformat()
        ),
        "detection_latency_ms": incident.detection_latency_ms,
        "duration_ms": incident.duration_ms,
        "zone_ids": " ".join(sorted(incident.zone_ids)),
        "involved_track_ids": " ".join(str(t) for t in sorted(incident.involved_track_ids)),
        "tracker_epoch": incident.tracker_epoch,
        "operator_note": incident.operator_note,
        "evidence_status": row.evidence_status,
        "evidence_count": len(row.evidence_ids),
        "evidence_ids": " ".join(row.evidence_ids),
        "evidence_paths": " ".join(row.evidence_paths),
        "trigger_summary": incident.explanation.trigger_summary,
        "model_name": model.name,
        "model_backend": model.backend,
        "model_device": model.device,
        "config_hash": incident.system_metadata.config_hash,
    }
    return [_cell(values[column]) for column in INCIDENT_COLUMNS]


def write_csv(report: Report, handle: TextIO) -> int:
    """Stream the incident rows as CSV. Returns the number of data rows written.

    The window, the filter and the caveats go out as `#` comment lines first: a CSV has nowhere
    else to put them, and a file of numbers with no statement of what they cover is the thing
    this whole module exists to avoid. A reader that ignores comments still gets a clean table.
    """
    for line in _csv_preamble(report):
        handle.write(f"# {line}\n")
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(INCIDENT_COLUMNS)
    written = 0
    for row in _stream(report.incidents):
        writer.writerow(incident_cells(row))
        written += 1
    return written


def _stream(rows: Sequence[IncidentRow]) -> Iterator[IncidentRow]:
    """A generator, so a large export costs one row of memory rather than all of them."""
    yield from rows


def _csv_preamble(report: Report) -> list[str]:
    summary = report.summary
    lines = [
        "VIGIL-88 incident export",
        f"generated_wall_utc={report.generated_wall_utc.isoformat()}",
        f"window={report.window.since.isoformat()}..{report.window.until.isoformat()}",
        f"incidents_total={summary.incidents_total} rows_included={len(report.incidents)}",
        f"truncated={'yes' if report.truncated else 'no'}",
        f"covers_full_window={'yes' if report.covers_full_window else 'no'}",
    ]
    filters = filter_json(report.filters)
    active = {k: v for k, v in filters.items() if v not in (None, [], False)}
    lines.append(f"filters={json.dumps(active, sort_keys=True) if active else 'none'}")
    lines.extend(f"caveat: {text}" for text in report.caveats)
    return lines


def write_summary_csv(report: Report, handle: TextIO) -> int:
    """The aggregate half as a long table: metric, key, value. Rows, not columns, because the
    set of keys is data-dependent and a wide table would change shape between exports."""
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(("metric", "key", "value", "unit", "population"))
    summary = report.summary
    written = 0
    groups: tuple[tuple[str, Sequence[Count]], ...] = (
        ("by_event_type", summary.by_event_type),
        ("by_severity", summary.by_severity),
        ("by_status", summary.by_status),
        ("by_camera", summary.by_camera),
        ("trend", summary.trend),
        ("candidate_outcomes", summary.candidate_outcomes),
        ("rejection_reasons", summary.rejection_reasons),
    )
    definitions = report.definitions
    for name, counts in groups:
        definition = definitions.get(name)
        for count in counts:
            writer.writerow(
                (
                    name,
                    _cell(count.key),
                    _cell(count.count),
                    definition.unit if definition else "",
                    definition.population if definition else "",
                )
            )
            written += 1
    for name, stat in (
        ("duration", summary.duration),
        ("detection_latency", summary.detection_latency),
        ("acknowledgement", summary.acknowledgement),
        ("resolution", summary.resolution),
    ):
        definition = definitions.get(name)
        for field_name, value in (
            ("count", stat.count),
            ("mean_ms", stat.mean_ms),
            ("p50_ms", stat.p50_ms),
            ("p95_ms", stat.p95_ms),
            ("max_ms", stat.max_ms),
        ):
            writer.writerow(
                (
                    name,
                    field_name,
                    # Empty, not 0: the metric was not computable for this population.
                    _cell(value),
                    definition.unit if definition else "",
                    definition.population if definition else "",
                )
            )
            written += 1
    return written


# ---------------------------------------------------------------- files


def write_report(
    report: Report,
    *,
    exports_dir: Path,
    filename: str,
    export_format: ExportFormat,
) -> ExportResult:
    """Write a report to a file inside `exports_dir`. Never writes outside it.

    The file is written to a temporary name and moved into place, so an interrupted export
    cannot leave a half-written file that looks complete.
    """
    target = export_path(exports_dir, filename)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PersistenceError(
            f"cannot create the exports directory {exports_dir}", context={"error": str(exc)}
        ) from exc
    reason = format_unavailable_reason(export_format)
    if reason is not None:
        raise PersistenceError(f"cannot export as {export_format.value}", context={"why": reason})
    temp = target.with_name(target.name + ".part")
    try:
        if export_format is ExportFormat.PDF:
            # Binary, and written through the same .part-then-replace dance: a reader that opens
            # the file only ever sees a complete document.
            with temp.open("wb") as binary:
                rows = pdf.write_pdf(report, binary)
        else:
            with temp.open("w", encoding="utf-8", newline=CSV_NEWLINE) as handle:
                rows = (
                    write_json(report, handle)
                    if export_format is ExportFormat.JSON
                    else write_csv(report, handle)
                )
        temp.replace(target)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise PersistenceError(
            f"cannot write the export to {target.name}",
            context={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    return ExportResult(
        path=target,
        format=export_format,
        rows=rows,
        bytes_written=target.stat().st_size,
    )
