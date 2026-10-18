"""A report as a printable PDF (10 §8, 02 §2).

`reportlab` is an **optional extra**, not a core dependency: `03 §1` rule 1 says every
dependency is a liability and a renderer is a large one bought for formatting. So this module
imports it lazily and reports its own availability, which is the same pattern the alert channels
use -- a format that cannot be produced says so, with the command that would fix it, and is
never silently answered with something else (ADR 0014 §C).

What the document contains is what the JSON and CSV exports contain, in the same order, because
three formats that disagree about the same window would be worse than two:

  * the window, the filters in force, and when it was generated;
  * the summary metrics, with **"unavailable" printed where a metric could not be computed** --
    not a zero, and not a blank that reads as one;
  * the data-generated caveats, verbatim;
  * one block per incident: reference, camera, type, severity, status, timings, the trigger
    summary, the operator's own note, the transition timeline, and **references** to evidence.

Evidence bytes are never embedded. A PDF with a frame in it is a copy of evidence outside the
store, with no hash check on the way out and nothing to verify it against later; the report
carries ids, relative paths and the capture status instead, and the API remains the only way to
fetch a file (05 §6).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from typing import BinaryIO, Protocol

from vigil.analytics.definitions import Count, DurationStat
from vigil.analytics.reports import IncidentRow, Report, Summary
from vigil.analytics.serialise import filter_json

INSTALL_HINT = "uv sync --extra reports"

# A4 in points, and a plain measured margin. Hard-coded rather than configurable: a report's
# page size is not an operational decision, and `reportlab` is only imported when it is used.
PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
MARGIN = 48.0
LEADING = 12.0
TITLE_SIZE = 15.0
HEADING_SIZE = 10.0
BODY_SIZE = 8.5
MONO = "Courier"
SANS = "Helvetica"
SANS_BOLD = "Helvetica-Bold"
# Characters that fit a line of Courier at BODY_SIZE within the margins. Courier is metrically
# fixed at 0.6 em, which is the only reason a character count is a safe way to wrap here.
WRAP_COLUMNS = int((PAGE_WIDTH - 2 * MARGIN) / (BODY_SIZE * 0.6))


def is_available() -> bool:
    """Whether a PDF can actually be produced right now.

    Checked by import machinery rather than by importing: this is called to decide what to *tell*
    an operator, including on a request that will not render anything, and loading a renderer to
    answer a question about availability would be the wrong cost in the wrong place.
    """
    return find_spec("reportlab") is not None


def unavailable_reason() -> str | None:
    if is_available():
        return None
    return (
        "PDF export needs the optional 'reports' extra, which is not installed. "
        f"Install it with: {INSTALL_HINT}. JSON and CSV export need nothing extra."
    )


class _Canvas(Protocol):
    """The slice of `reportlab`'s canvas this module uses, so the call sites stay typed."""

    def setFont(self, name: str, size: float) -> None: ...
    def drawString(self, x: float, y: float, text: str) -> None: ...
    def setLineWidth(self, width: float) -> None: ...
    def line(self, x1: float, y1: float, x2: float, y2: float) -> None: ...
    def showPage(self) -> None: ...
    def save(self) -> None: ...
    def setTitle(self, title: str) -> None: ...


@dataclass(slots=True)
class _Cursor:
    """Where the next line goes, and the page breaks, kept out of the content functions."""

    canvas: _Canvas
    y: float = PAGE_HEIGHT - MARGIN

    def line(self, text: str, *, font: str = MONO, size: float = BODY_SIZE) -> None:
        if self.y < MARGIN + LEADING:
            self.canvas.showPage()
            self.y = PAGE_HEIGHT - MARGIN
        self.canvas.setFont(font, size)
        self.canvas.drawString(MARGIN, self.y, text)
        self.y -= LEADING

    def heading(self, text: str) -> None:
        self.gap(4.0)
        self.line(text, font=SANS_BOLD, size=HEADING_SIZE)
        self.rule()

    def rule(self) -> None:
        self.canvas.setLineWidth(0.4)
        self.canvas.line(MARGIN, self.y + 3.0, PAGE_WIDTH - MARGIN, self.y + 3.0)
        self.y -= 3.0

    def gap(self, points: float = LEADING / 2) -> None:
        self.y -= points

    def wrapped(self, label: str, text: str) -> None:
        """Wrap at a column count, which is exact for Courier and only for Courier."""
        prefix = f"{label}: "
        width = max(WRAP_COLUMNS - len(prefix), 20)
        remaining = text
        first = True
        while remaining:
            chunk, remaining = remaining[:width], remaining[width:]
            self.line(f"{prefix if first else ' ' * len(prefix)}{chunk}")
            first = False


def _duration_line(label: str, stat: DurationStat) -> str:
    """Unavailable is printed as unavailable. A blank in a table of numbers reads as zero."""
    if not stat.available or stat.p50_ms is None or stat.p95_ms is None:
        return f"{label:<22} unavailable (no incident in this window has this measurement)"
    return (
        f"{label:<22} p50 {stat.p50_ms / 1000:7.1f}s   p95 {stat.p95_ms / 1000:7.1f}s   "
        f"n={stat.count}"
    )


def _count_lines(counts: Sequence[Count]) -> Iterator[str]:
    if not counts:
        yield "  (no data in this window)"
        return
    width = max(len(c.key) for c in counts)
    for c in counts:
        yield f"  {c.key:<{width}}  {c.count}"


def _summary_block(cursor: _Cursor, report: Report) -> None:
    summary: Summary = report.summary
    cursor.heading("Summary")
    cursor.line(f"incidents in window     {summary.incidents_total}")
    cursor.line(f"open now                {summary.open_now}")
    cursor.line(_duration_line("detection latency", summary.detection_latency))
    cursor.line(_duration_line("time to acknowledge", summary.acknowledgement))
    cursor.line(_duration_line("time to resolve", summary.resolution))
    cursor.line(_duration_line("incident duration", summary.duration))

    for title, counts in (
        ("By event type", summary.by_event_type),
        ("By severity", summary.by_severity),
        ("By status", summary.by_status),
        ("By camera", summary.by_camera),
    ):
        cursor.gap()
        cursor.line(title, font=SANS_BOLD, size=BODY_SIZE)
        for text in _count_lines(counts):
            cursor.line(text)

    cursor.gap()
    cursor.line("Dismissal rate by module version", font=SANS_BOLD, size=BODY_SIZE)
    if not summary.dismissal_rate:
        cursor.line("  (no incidents in this window)")
    else:
        for module_id, version, dismissed, total, ratio in summary.dismissal_rate:
            cursor.line(f"  {module_id} {version}: {dismissed}/{total} ({ratio * 100:.1f}%)")
        cursor.line("  this is the dismissal rate, not the detector's false-positive rate")

    cursor.gap()
    cursor.line("Camera availability", font=SANS_BOLD, size=BODY_SIZE)
    if not summary.camera_availability:
        cursor.line("  (no health history recorded)")
    for camera, totals in summary.camera_availability.items():
        if totals is None:
            # Unknown, never 100%: a blind camera must not read as a healthy one.
            cursor.line(f"  {camera}: unknown (no transitions recorded)")
            continue
        seconds = sum(totals.values()) or 1.0
        cursor.line(f"  {camera}: {(totals.get('online', 0.0) / seconds) * 100:.1f}% online")


def _incident_block(cursor: _Cursor, row: IncidentRow) -> None:
    incident = row.incident
    model = incident.model_metadata
    cursor.gap()
    cursor.line(
        f"{incident.display_ref}  {incident.severity.name}  {incident.event_type.value}",
        font=SANS_BOLD,
        size=BODY_SIZE,
    )
    cursor.line(
        f"  camera {incident.camera_id}"
        f"{f' ({incident.location})' if incident.location else ''}"
        f"   status {incident.status.value}"
        f"   confidence {incident.confidence_ratio:.2f}"
        f"   severity score {incident.severity_score:.2f}"
    )
    cursor.line(f"  confirmed   {incident.confirmed_wall_utc.isoformat()}")
    cursor.line(
        f"  first/last  {incident.first_observed_wall_utc.isoformat()} .. "
        f"{incident.last_observed_wall_utc.isoformat()}"
    )
    if incident.resolved_wall_utc is not None:
        cursor.line(f"  resolved    {incident.resolved_wall_utc.isoformat()}")
    # Always computable: it is the gap between two stored timestamps (06 §9).
    cursor.line(f"  detection latency {incident.detection_latency_ms:.0f} ms")
    cursor.wrapped("  trigger", incident.explanation.trigger_summary)
    if incident.operator_note:
        cursor.wrapped("  note", incident.operator_note)
    cursor.line(
        f"  module {incident.module_id} {incident.module_version}"
        f"   model {model.name} ({model.backend}/{model.device})"
    )
    if incident.zone_ids:
        cursor.line(f"  zones {' '.join(sorted(incident.zone_ids))}")

    status = row.evidence_status or "none"
    cursor.line(f"  evidence {len(row.evidence_ids)} item(s), status {status}")
    for evidence_id, path in zip(row.evidence_ids, row.evidence_paths, strict=False):
        # A reference, never the bytes: see the module docstring.
        cursor.line(f"    {evidence_id}  {path}")

    if incident.transitions:
        cursor.line("  timeline")
        for transition in incident.transitions:
            # The first transition has no `from_status`: the incident did not exist before it.
            came_from = "(new)" if transition.from_status is None else transition.from_status.value
            cursor.line(
                f"    {transition.at_wall_utc.isoformat()}  "
                f"{came_from} -> {transition.to_status.value}  "
                f"[{transition.actor}]  {transition.reason}"
            )
    else:
        cursor.line("  timeline unavailable (no transitions recorded for this incident)")


def write_pdf(report: Report, handle: BinaryIO) -> int:
    """Render the report. Returns the number of incident blocks written.

    Raises `ModuleNotFoundError` when the extra is absent; callers ask `is_available()` first and
    report `unavailable_reason()` instead of letting the import error reach an operator.
    """
    from reportlab.pdfgen.canvas import Canvas  # imported here: optional extra

    canvas: _Canvas = Canvas(handle, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    canvas.setTitle("VIGIL-88 incident report")
    cursor = _Cursor(canvas)

    cursor.line("VIGIL-88 incident report", font=SANS_BOLD, size=TITLE_SIZE)
    cursor.rule()
    cursor.line(f"generated   {report.generated_wall_utc.isoformat()}", font=SANS, size=BODY_SIZE)
    cursor.line(
        f"window      {report.window.since.isoformat()} .. {report.window.until.isoformat()} "
        f"(closed-open)",
        font=SANS,
        size=BODY_SIZE,
    )
    active = {k: v for k, v in filter_json(report.filters).items() if v not in (None, [], False)}
    cursor.wrapped(
        "filters",
        ", ".join(f"{k}={v}" for k, v in sorted(active.items())) if active else "none",
    )
    cursor.line(
        f"coverage    {'full window' if report.covers_full_window else 'partial'}"
        f"   earliest record "
        f"{report.earliest_record.isoformat() if report.earliest_record else 'none'}"
    )
    cursor.line(
        f"rows        {len(report.incidents)} of {report.summary.incidents_total}"
        f"{'  (truncated)' if report.truncated else ''}"
    )

    if report.caveats:
        cursor.heading("Caveats")
        for text in report.caveats:
            cursor.wrapped("  -", text)

    _summary_block(cursor, report)

    cursor.heading(f"Incidents ({len(report.incidents)})")
    if not report.incidents:
        cursor.line("  no incidents matched this window and filter")
    written = 0
    for row in report.incidents:
        _incident_block(cursor, row)
        written += 1

    cursor.heading("Metric definitions")
    for name, definition in sorted(report.definitions.items()):
        cursor.wrapped(f"  {name}", f"{definition.definition} [{definition.population}]")

    canvas.showPage()
    canvas.save()
    return written
