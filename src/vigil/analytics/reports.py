"""Composing a report from persisted records (10-roadmap §8).

A report is a **statement about a window and a filter**, so it carries both, plus the moment it
was generated and what the data actually covers. Without those four, two reports with different
numbers are indistinguishable from one report that changed its mind.

Three properties the composition is responsible for:

  * **The contents match the stated filter.** The summary and the incident list are built from
    the same `IncidentFilter` and the same `Window`, through the same query builder, so the
    count at the top cannot disagree with the rows underneath. That is the single easiest way
    for a report to lie, and it is prevented by construction rather than by review.
  * **Coverage is declared.** `covers_full_window` is false when the window starts before the
    oldest record, and `truncated` is true when the row cap was reached. A report that quietly
    showed the first thousand of four thousand incidents would be worse than one that refused.
  * **An empty result is a result.** Zero incidents in a window is a fact about that window,
    and the report says so rather than failing or omitting sections.

Nothing is modelled or extrapolated. Every number comes from `analytics/queries.py`, which
aggregates in SQL over indexed columns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from vigil.analytics.queries import (
    DEFINITIONS,
    AnalyticsQueries,
    Bucket,
    Count,
    DurationStat,
    MetricDefinition,
    Window,
)
from vigil.domain.incident import Incident
from vigil.persistence.queries import ALL_INCIDENTS, IncidentFilter, clamp_page_size
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.persistence.repositories.incidents import IncidentRepository

DEFAULT_ROW_LIMIT = 500
MAX_ROW_LIMIT = 10_000


@dataclass(frozen=True, slots=True)
class Summary:
    """The aggregate half. Every duration may be unavailable rather than zero."""

    incidents_total: int
    open_now: int
    by_event_type: tuple[Count, ...]
    by_severity: tuple[Count, ...]
    by_status: tuple[Count, ...]
    by_camera: tuple[Count, ...]
    trend: tuple[Count, ...]
    trend_bucket: Bucket
    duration: DurationStat
    detection_latency: DurationStat
    acknowledgement: DurationStat
    resolution: DurationStat
    dismissal_rate: tuple[tuple[str, str, int, int, float], ...]
    candidate_outcomes: tuple[Count, ...]
    rejection_reasons: tuple[Count, ...]
    camera_availability: Mapping[str, Mapping[str, float] | None]


@dataclass(frozen=True, slots=True)
class IncidentRow:
    """One incident as a report sees it: the summary fields plus its evidence references.

    Evidence is referenced, never embedded. A report is a document about incidents; the files
    are fetched through the API by `evidence_id`, which is also the only path that re-checks
    the stored relative path and verifies the hash (05 §6).
    """

    incident: Incident
    evidence_ids: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    evidence_status: str | None


@dataclass(frozen=True, slots=True)
class Report:
    generated_wall_utc: datetime
    window: Window
    filters: IncidentFilter
    summary: Summary
    incidents: tuple[IncidentRow, ...]
    definitions: Mapping[str, MetricDefinition]
    covers_full_window: bool
    earliest_record: datetime | None
    truncated: bool
    row_limit: int
    caveats: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return self.summary.incidents_total == 0


class ReportBuilder:
    """Builds a report. One query per metric, one page of rows, nothing held open."""

    __slots__ = ("_analytics", "_evidence", "_incidents")

    def __init__(
        self,
        *,
        analytics: AnalyticsQueries,
        incidents: IncidentRepository,
        evidence: EvidenceRepository,
    ) -> None:
        self._analytics = analytics
        self._incidents = incidents
        self._evidence = evidence

    def summary(
        self,
        window: Window,
        *,
        filters: IncidentFilter = ALL_INCIDENTS,
        bucket: Bucket = Bucket.DAY,
    ) -> Summary:
        analytics = self._analytics
        return Summary(
            incidents_total=analytics.total(window, filters),
            open_now=analytics.open_incidents(),
            by_event_type=analytics.by_event_type(window, filters),
            by_severity=analytics.by_severity(window, filters),
            by_status=analytics.by_status(window, filters),
            by_camera=analytics.by_camera(window, filters),
            trend=analytics.trend(window, bucket, filters),
            trend_bucket=bucket,
            duration=analytics.duration(window, filters),
            detection_latency=analytics.detection_latency(window, filters),
            acknowledgement=analytics.acknowledgement_time(window, filters),
            resolution=analytics.resolution_time(window, filters),
            dismissal_rate=analytics.dismissal_rate(window, filters),
            candidate_outcomes=analytics.candidate_outcomes(window),
            rejection_reasons=analytics.rejection_reasons(window),
            camera_availability=analytics.camera_availability(window),
        )

    def build(
        self,
        window: Window,
        *,
        filters: IncidentFilter = ALL_INCIDENTS,
        bucket: Bucket = Bucket.DAY,
        row_limit: int | None = None,
        generated_wall_utc: datetime,
        include_incidents: bool = True,
    ) -> Report:
        limit = clamp_page_size(row_limit, default=DEFAULT_ROW_LIMIT, maximum=MAX_ROW_LIMIT)
        summary = self.summary(window, filters=filters, bucket=bucket)
        rows: tuple[IncidentRow, ...] = ()
        truncated = False
        if include_incidents:
            rows = self._rows(window, filters, limit)
            truncated = summary.incidents_total > len(rows)

        earliest = self._analytics.earliest_incident()
        # "Covered" means there is data for the *whole* window, which requires the oldest record
        # to be at or before its start. The inverse case -- a window reaching back before the
        # data begins -- is the one a reader has to be told about, because the empty early part
        # looks like a quiet period rather than an absence of records.
        covers = earliest is not None and earliest <= window.since
        return Report(
            generated_wall_utc=generated_wall_utc,
            window=window,
            filters=filters,
            summary=summary,
            incidents=rows,
            definitions=DEFINITIONS,
            covers_full_window=covers,
            earliest_record=earliest,
            truncated=truncated,
            row_limit=limit,
            caveats=self._caveats(summary, earliest=earliest, window=window, truncated=truncated),
        )

    def _rows(self, window: Window, filters: IncidentFilter, limit: int) -> tuple[IncidentRow, ...]:
        scoped = IncidentFilter(
            camera_ids=filters.camera_ids,
            event_types=filters.event_types,
            statuses=filters.statuses,
            min_severity=filters.min_severity,
            since_wall_utc=window.since,
            until_wall_utc=window.until,
            zone_id=filters.zone_id,
            search=filters.search,
            open_only=filters.open_only,
            has_evidence=filters.has_evidence,
        )
        page = self._incidents.page(scoped, limit=limit)
        ids = [i.incident_id for i in page.incidents]
        # One query each for the page's transitions and evidence counts, rather than two per
        # row: a 500-row report would otherwise make a thousand round trips.
        timelines = self._incidents.transitions_for(ids)
        out: list[IncidentRow] = []
        for incident in page.incidents:
            records = self._evidence.for_incident(incident.incident_id)
            out.append(
                IncidentRow(
                    # `page()` returns incidents without their transitions; a report needs the
                    # timeline, so it is attached here.
                    incident=replace(incident, transitions=timelines.get(incident.incident_id, ())),
                    evidence_ids=tuple(r.evidence_id for r in records),
                    evidence_paths=tuple(r.relative_path for r in records),
                    evidence_status=self._incidents.evidence_status(incident.incident_id),
                )
            )
        return tuple(out)

    @staticmethod
    def _caveats(
        summary: Summary,
        *,
        earliest: datetime | None,
        window: Window,
        truncated: bool,
    ) -> tuple[str, ...]:
        """What a reader has to know to not misread the numbers above.

        These are generated from the data, not written once and hoped over: each appears only
        when the condition that makes it necessary actually holds.
        """
        out: list[str] = []
        if summary.incidents_total == 0:
            out.append(
                "No incidents match this window and filter. That is a fact about the window, "
                "not a failure to query."
            )
        if earliest is None:
            out.append("The incident store is empty, so no window can be covered.")
        elif earliest > window.since:
            out.append(
                f"The oldest stored incident is {earliest.isoformat()}, after the start of this "
                "window: the earlier part of the window predates the data."
            )
        if truncated:
            out.append(
                f"{summary.incidents_total} incidents match, and only the first page is listed. "
                "The summary above counts them all; narrow the window or raise the row limit to "
                "list more."
            )
        if summary.dismissal_rate:
            out.append(
                "The dismissal rate is the fraction an operator marked as a false positive. It "
                "is not the detector's false-positive rate: it omits false positives nobody "
                "dismissed and true positives nobody acknowledged."
            )
        unknown = [c for c, totals in summary.camera_availability.items() if totals is None]
        if unknown:
            out.append(
                "Availability is unknown for "
                + ", ".join(sorted(unknown))
                + ": no health transitions were recorded for them in this window."
            )
        if not summary.acknowledgement.available and summary.incidents_total:
            out.append(
                "No incident in this window was acknowledged, so acknowledgement time is "
                "unavailable rather than zero."
            )
        out.append(
            "Confidence values are the temporal engine's accumulated log-odds mapped to [0, 1]. "
            "They rank evidence strength and have not been calibrated against outcomes, so they "
            "are not probabilities."
        )
        return tuple(out)


def definitions_for(names: Sequence[str]) -> dict[str, MetricDefinition]:
    """The definitions behind a chosen subset of metrics, for a narrower export."""
    return {name: DEFINITIONS[name] for name in names if name in DEFINITIONS}
