"""Analytics and exports (08 §3 Analytics, §11).

Two things this router is careful about.

**It never invents a window.** A caller gives a range, or gets the default one, and the response
echoes back exactly what was used along with what the data covers. An analytics screen whose
numbers cannot be traced to a stated window and filter is a screen nobody can check.

**An export is a file write, so the filename is validated, not trusted.** The caller chooses a
format and may suggest a name; the name is reduced to a plain filename and resolved inside the
configured exports directory. Nothing a caller sends can name a directory or climb out of one.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from vigil.analytics.export import (
    ExportFormat,
    format_unavailable_reason,
    write_csv,
    write_report,
)
from vigil.analytics.pdf import write_pdf
from vigil.analytics.queries import AnalyticsQueries, Bucket, Count, DurationStat, Window
from vigil.analytics.reports import Report, ReportBuilder
from vigil.analytics.serialise import report_to_json, write_json
from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.core.ids import UlidFactory
from vigil.domain.enums import EventType, IncidentStatus, Severity
from vigil.persistence.queries import IncidentFilter
from vigil.response.paths import SAFE_COMPONENT

DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 366 * 5
MAX_OFFSET_MINUTES = 14 * 60


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class CountOut(_Out):
    key: str
    count: int


class ExportFormatOut(_Out):
    """One export format and whether this install can produce it.

    `reason` is the install command when a renderer is missing, so the answer is actionable
    rather than merely negative (D-008).
    """

    format: str
    available: bool
    reason: str | None


class DurationOut(_Out):
    """`available: false` with null statistics means the population was empty, not that the
    duration was zero (08 §8)."""

    available: bool
    count: int
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None

    @classmethod
    def of(cls, stat: DurationStat) -> DurationOut:
        return cls(
            available=stat.available,
            count=stat.count,
            mean_ms=stat.mean_ms,
            p50_ms=stat.p50_ms,
            p95_ms=stat.p95_ms,
            max_ms=stat.max_ms,
        )


class DismissalRateOut(_Out):
    module_id: str
    module_version: str
    dismissed: int
    total: int
    ratio: float


class AnalyticsSummaryOut(_Out):
    since_utc: str
    until_utc: str
    utc_offset_minutes: int
    bucket: str
    covers_full_window: bool
    earliest_record_utc: str | None
    incidents_total: int
    open_now: int
    by_event_type: list[CountOut]
    by_severity: list[CountOut]
    by_status: list[CountOut]
    by_camera: list[CountOut]
    trend: list[CountOut]
    duration: DurationOut
    detection_latency: DurationOut
    acknowledgement: DurationOut
    resolution: DurationOut
    dismissal_rate: list[DismissalRateOut]
    candidate_outcomes: list[CountOut]
    rejection_reasons: list[CountOut]
    camera_availability: dict[str, dict[str, float] | None]
    caveats: list[str]
    metric_definitions: dict[str, dict[str, str]]


def _counts(counts: tuple[Count, ...]) -> list[CountOut]:
    return [CountOut(key=c.key, count=c.count) for c in counts]


def build_router(
    *,
    analytics: AnalyticsQueries,
    reports: ReportBuilder,
    exports_dir: Path,
    clock: Clock,
    ulids: UlidFactory,
) -> APIRouter:
    router = APIRouter(prefix="/analytics", tags=["analytics"])

    def _window(
        since: datetime | None,
        until: datetime | None,
        days: int,
        utc_offset_minutes: int,
    ) -> Window:
        end = until or clock.wall_utc()
        start = since or (end - timedelta(days=days))
        try:
            return Window(since=start, until=end, utc_offset_minutes=utc_offset_minutes)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    def _filters(
        camera_id: str | None,
        event_type: str | None,
        incident_status: str | None,
        min_severity: str | None,
    ) -> IncidentFilter:
        try:
            return IncidentFilter(
                camera_ids=tuple(c.strip() for c in (camera_id or "").split(",") if c.strip()),
                event_types=tuple(
                    EventType(t.strip()) for t in (event_type or "").split(",") if t.strip()
                ),
                statuses=tuple(
                    IncidentStatus(s.strip())
                    for s in (incident_status or "").split(",")
                    if s.strip()
                ),
                min_severity=(None if not min_severity else Severity[min_severity.strip().upper()]),
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=f"unknown filter value: {exc}") from None

    @router.get("/summary", response_model=AnalyticsSummaryOut)
    def summary(
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
        days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = DEFAULT_WINDOW_DAYS,
        bucket: Annotated[Bucket, Query()] = Bucket.DAY,
        utc_offset_minutes: Annotated[
            int, Query(ge=-MAX_OFFSET_MINUTES, le=MAX_OFFSET_MINUTES)
        ] = 0,
        camera_id: Annotated[str | None, Query(max_length=400)] = None,
        event_type: Annotated[str | None, Query(max_length=400)] = None,
        incident_status: Annotated[str | None, Query(alias="status", max_length=200)] = None,
        min_severity: Annotated[str | None, Query(max_length=20)] = None,
    ) -> AnalyticsSummaryOut:
        window = _window(since, until, days, utc_offset_minutes)
        filters = _filters(camera_id, event_type, incident_status, min_severity)
        try:
            report = reports.build(
                window,
                filters=filters,
                bucket=bucket,
                generated_wall_utc=clock.wall_utc(),
                include_incidents=False,
            )
        except PersistenceError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from None
        s = report.summary
        return AnalyticsSummaryOut(
            since_utc=window.since.isoformat(),
            until_utc=window.until.isoformat(),
            utc_offset_minutes=window.utc_offset_minutes,
            bucket=bucket.value,
            covers_full_window=report.covers_full_window,
            earliest_record_utc=(
                None if report.earliest_record is None else report.earliest_record.isoformat()
            ),
            incidents_total=s.incidents_total,
            open_now=s.open_now,
            by_event_type=_counts(s.by_event_type),
            by_severity=_counts(s.by_severity),
            by_status=_counts(s.by_status),
            by_camera=_counts(s.by_camera),
            trend=_counts(s.trend),
            duration=DurationOut.of(s.duration),
            detection_latency=DurationOut.of(s.detection_latency),
            acknowledgement=DurationOut.of(s.acknowledgement),
            resolution=DurationOut.of(s.resolution),
            dismissal_rate=[
                DismissalRateOut(
                    module_id=module_id,
                    module_version=version,
                    dismissed=dismissed,
                    total=total,
                    ratio=ratio,
                )
                for module_id, version, dismissed, total, ratio in s.dismissal_rate
            ],
            candidate_outcomes=_counts(s.candidate_outcomes),
            rejection_reasons=_counts(s.rejection_reasons),
            camera_availability={
                camera: (None if totals is None else dict(totals))
                for camera, totals in s.camera_availability.items()
            },
            caveats=list(report.caveats),
            metric_definitions={
                name: {
                    "population": d.population,
                    "definition": d.definition,
                    "unit": d.unit,
                }
                for name, d in sorted(report.definitions.items())
            },
        )

    @router.get("/report")
    def report_json(
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
        days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = DEFAULT_WINDOW_DAYS,
        bucket: Annotated[Bucket, Query()] = Bucket.DAY,
        rows: Annotated[int | None, Query(ge=1)] = None,
        camera_id: Annotated[str | None, Query(max_length=400)] = None,
        event_type: Annotated[str | None, Query(max_length=400)] = None,
        incident_status: Annotated[str | None, Query(alias="status", max_length=200)] = None,
        min_severity: Annotated[str | None, Query(max_length=20)] = None,
    ) -> dict[str, object]:
        """The full report, incidents included, as the same JSON an export writes to a file."""
        window = _window(since, until, days, 0)
        filters = _filters(camera_id, event_type, incident_status, min_severity)
        try:
            report = reports.build(
                window,
                filters=filters,
                bucket=bucket,
                row_limit=rows,
                generated_wall_utc=clock.wall_utc(),
            )
        except PersistenceError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from None
        return report_to_json(report)

    @router.get("/export")
    def export(
        export_format: Annotated[ExportFormat, Query(alias="format")] = ExportFormat.CSV,
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
        days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = DEFAULT_WINDOW_DAYS,
        rows: Annotated[int | None, Query(ge=1)] = None,
        camera_id: Annotated[str | None, Query(max_length=400)] = None,
        event_type: Annotated[str | None, Query(max_length=400)] = None,
        incident_status: Annotated[str | None, Query(alias="status", max_length=200)] = None,
        min_severity: Annotated[str | None, Query(max_length=20)] = None,
        save: Annotated[bool, Query()] = False,
        name: Annotated[str | None, Query(max_length=64)] = None,
    ) -> Response:
        """Download the export, and optionally also keep a copy under `paths.exports_dir`.

        Downloading is the default because that is what an operator wants; saving is opt-in
        because it is the only operation here that touches the filesystem.
        """
        window = _window(since, until, days, 0)
        filters = _filters(camera_id, event_type, incident_status, min_severity)
        try:
            report = reports.build(
                window,
                filters=filters,
                row_limit=rows,
                generated_wall_utc=clock.wall_utc(),
            )
        except PersistenceError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from None

        body, media_type = _render(report, export_format)

        filename = _export_name(name, export_format, ulids)
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        if save:
            try:
                result = write_report(
                    report,
                    exports_dir=exports_dir,
                    filename=filename,
                    export_format=export_format,
                )
            except PersistenceError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from None
            headers["X-Export-Saved-As"] = result.path.name
        return Response(body, media_type=media_type, headers=headers)

    @router.get("/formats", response_model=list[ExportFormatOut])
    def formats() -> list[ExportFormatOut]:
        """Which export formats this install can actually produce, and why not where it cannot.

        The viewer reads this to decide whether a button is enabled, so that a control is never
        offered and then answered with an error (08 §8).
        """
        return [
            ExportFormatOut(
                format=candidate.value,
                available=(reason := format_unavailable_reason(candidate)) is None,
                reason=reason,
            )
            for candidate in ExportFormat
        ]

    return router


def _render(report: Report, export_format: ExportFormat) -> tuple[bytes, str]:
    """Produce the body, or a 503 naming what is missing and how to install it.

    503 rather than 422: the request was valid and the format is a real one. What is absent is a
    capability of this deployment, which is a different thing from a bad parameter and deserves
    a different answer.
    """
    reason = format_unavailable_reason(export_format)
    if reason is not None:
        raise HTTPException(status_code=503, detail=reason)
    if export_format is ExportFormat.PDF:
        binary = io.BytesIO()
        write_pdf(report, binary)
        return binary.getvalue(), "application/pdf"
    buffer = io.StringIO()
    if export_format is ExportFormat.JSON:
        write_json(report, buffer)
        media_type = "application/json"
    else:
        write_csv(report, buffer)
        media_type = "text/csv"
    return buffer.getvalue().encode("utf-8"), media_type


def _export_name(requested: str | None, export_format: ExportFormat, ulids: UlidFactory) -> str:
    """A safe filename, from the caller's suggestion where it is usable and ours where it is not.

    Validated rather than sanitised-and-hoped: a name that is not a plain safe component is
    replaced outright, so there is no partially-cleaned string to reason about.
    """
    stem = (requested or "").strip()
    if not stem or not SAFE_COMPONENT.match(stem) or "." in stem:
        stem = f"incidents-{ulids.new()}"
    return f"{stem}.{export_format.value}"
