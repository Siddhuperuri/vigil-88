"""Aggregations over persisted records (08 §3 Analytics, 10-roadmap §8).

Three rules, and the third is the one that makes this layer trustworthy.

**1. Aggregate in SQL, not in Python.** Every function here returns a handful of rows from a
`GROUP BY` against an indexed column. Nothing loads the incident table into memory to count it;
`03 §8` rejected pandas on exactly this reasoning, and a dashboard that needs the whole table to
draw a bar chart stops working at the moment the data becomes interesting.

**2. Every metric says how it was computed.** `MetricDefinition` travels with each result, so a
number on a screen can be traced to the population it came from. "Incidents" and "events" and
"candidates" are different populations (06 §1) and a figure that silently mixed them would be
worse than no figure.

**3. An uncomputable metric is `None`, never 0.** A camera with no recorded health history has
*unknown* availability, not 100% and not 0%. Acknowledgement time for incidents nobody
acknowledged is unknown, not instant. The types here are `| None` throughout for that reason,
and the UI renders "unavailable" (08 §8).

**On false-positive rate.** The dismissal rate is reported, and it is reported as *the dismissal
rate* — the fraction of incidents an operator marked as a false positive. That is not the
false-positive rate of the detector: it omits every false positive nobody got round to
dismissing, and every true positive nobody acknowledged. 06 §9 calls dismissal the only honest
*measure* available, and the wording here keeps it that honest.

**Time zones.** Every boundary is computed in UTC and every bucket key is an ISO-8601 UTC
string, because the database stores UTC and a bucket boundary that moved with a viewer's local
offset would make the same incident land in different days for different people. A caller that
wants local days passes an offset and gets buckets shifted by it, labelled with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from vigil.analytics.definitions import (
    DEFINITIONS,
    EMPTY_DURATION,
    Count,
    DurationStat,
    MetricDefinition,
)
from vigil.domain.enums import CandidateOutcome, EventType, IncidentStatus, Severity
from vigil.persistence.database import Database
from vigil.persistence.mappers import from_utc_text, to_utc_text
from vigil.persistence.queries import ALL_INCIDENTS, IncidentFilter, build_where

__all__ = [
    "DEFINITIONS",
    "MAX_BUCKETS",
    "AnalyticsQueries",
    "Bucket",
    "Count",
    "DurationStat",
    "MetricDefinition",
    "Window",
    "bucket_key",
    "bucket_keys",
]

MAX_BUCKETS = 1000


class Bucket(StrEnum):
    """Time granularity. SQLite's `substr` over an ISO-8601 UTC string is the bucket key, which
    is exact for these widths and needs no date arithmetic in SQL."""

    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


# How many characters of an ISO-8601 UTC timestamp identify each bucket.
_PREFIX: Mapping[Bucket, int] = {
    Bucket.HOUR: 13,  # 2026-03-01T02
    Bucket.DAY: 10,  # 2026-03-01
    Bucket.MONTH: 7,  # 2026-03
}


@dataclass(frozen=True, slots=True)
class Window:
    """A closed-open UTC interval, with the offset any local bucketing was computed at."""

    since: datetime
    until: datetime
    utc_offset_minutes: int = 0

    def __post_init__(self) -> None:
        if self.until <= self.since:
            raise ValueError("an analytics window must end after it starts")

    @property
    def duration_s(self) -> float:
        return (self.until - self.since).total_seconds()


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile over a sorted sequence. None for an empty population."""
    if not values:
        return None
    index = min(round(fraction * (len(values) - 1)), len(values) - 1)
    return values[index]


def _duration_stat(values: Sequence[float]) -> DurationStat:
    if not values:
        return EMPTY_DURATION
    ordered = sorted(values)
    return DurationStat(
        count=len(ordered),
        mean_ms=sum(ordered) / len(ordered),
        p50_ms=_percentile(ordered, 0.5),
        p95_ms=_percentile(ordered, 0.95),
        max_ms=ordered[-1],
    )


def bucket_key(moment: datetime, bucket: Bucket, *, utc_offset_minutes: int = 0) -> str:
    """The bucket a moment falls in, as a stable string. Weeks are keyed by their Monday."""
    shifted = moment + timedelta(minutes=utc_offset_minutes)
    if bucket is Bucket.WEEK:
        monday = shifted - timedelta(days=shifted.weekday())
        return monday.date().isoformat()
    return to_utc_text(shifted)[: _PREFIX[bucket]]


def bucket_keys(window: Window, bucket: Bucket) -> tuple[str, ...]:
    """Every bucket in the window, including the empty ones.

    Empty buckets are the point. A trend line drawn only from buckets that have rows implies
    continuity where there was none, and a quiet night would look like a missing night.
    """
    step = {
        Bucket.HOUR: timedelta(hours=1),
        Bucket.DAY: timedelta(days=1),
        Bucket.WEEK: timedelta(weeks=1),
        Bucket.MONTH: timedelta(days=28),
    }[bucket]
    keys: list[str] = []
    seen: set[str] = set()
    moment = window.since
    while moment < window.until and len(keys) < MAX_BUCKETS:
        key = bucket_key(moment, bucket, utc_offset_minutes=window.utc_offset_minutes)
        if key not in seen:
            seen.add(key)
            keys.append(key)
        moment += step
    # The final boundary can fall inside a bucket the loop never stepped onto.
    last = bucket_key(
        window.until - timedelta(microseconds=1),
        bucket,
        utc_offset_minutes=window.utc_offset_minutes,
    )
    if last not in seen and len(keys) < MAX_BUCKETS:
        keys.append(last)
    return tuple(keys)


class AnalyticsQueries:
    """Read-only aggregations. Holds no state; every method is one statement."""

    __slots__ = ("_db",)

    def __init__(self, database: Database) -> None:
        self._db = database

    # ---------------------------------------------------------------- counts

    def _where(self, window: Window, filters: IncidentFilter) -> tuple[str, list[object]]:
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
        clause = build_where(scoped, alias="incidents")
        return (clause.rendered, clause.params)

    def total(self, window: Window, filters: IncidentFilter = ALL_INCIDENTS) -> int:
        where, params = self._where(window, filters)
        return int(self._db.scalar(f"SELECT COUNT(*) FROM incidents{where}", params) or 0)

    def _grouped(self, column: str, window: Window, filters: IncidentFilter) -> tuple[Count, ...]:
        where, params = self._where(window, filters)
        rows = self._db.query(
            f"SELECT {column} AS k, COUNT(*) AS n FROM incidents{where} "
            f"GROUP BY {column} ORDER BY n DESC, k ASC",
            params,
        )
        return tuple(Count(key=str(r["k"]), count=int(r["n"])) for r in rows)

    def by_event_type(
        self, window: Window, filters: IncidentFilter = ALL_INCIDENTS
    ) -> tuple[Count, ...]:
        return self._grouped("event_type", window, filters)

    def by_camera(
        self, window: Window, filters: IncidentFilter = ALL_INCIDENTS
    ) -> tuple[Count, ...]:
        return self._grouped("camera_id", window, filters)

    def by_status(
        self, window: Window, filters: IncidentFilter = ALL_INCIDENTS
    ) -> tuple[Count, ...]:
        return self._grouped("status", window, filters)

    def by_severity(
        self, window: Window, filters: IncidentFilter = ALL_INCIDENTS
    ) -> tuple[Count, ...]:
        """Keyed by band name, so a chart legend reads INFO..CRITICAL rather than 0..4."""
        counts = self._grouped("severity", window, filters)
        return tuple(Count(key=Severity(int(c.key)).name, count=c.count) for c in counts)

    # ---------------------------------------------------------------- trend

    def trend(
        self,
        window: Window,
        bucket: Bucket,
        filters: IncidentFilter = ALL_INCIDENTS,
    ) -> tuple[Count, ...]:
        """Incidents per bucket, with empty buckets present and zero."""
        where, params = self._where(window, filters)
        offset = window.utc_offset_minutes
        if bucket is Bucket.WEEK or offset:
            # A week key needs weekday arithmetic, and a local offset shifts every boundary.
            # Both are done in Python over the timestamps the filter already narrowed to, which
            # is bounded by the window rather than by the table.
            rows = self._db.query(f"SELECT confirmed_utc FROM incidents{where}", params)
            tally: dict[str, int] = {}
            for row in rows:
                key = bucket_key(
                    from_utc_text(row["confirmed_utc"]), bucket, utc_offset_minutes=offset
                )
                tally[key] = tally.get(key, 0) + 1
        else:
            grouped = self._db.query(
                f"SELECT substr(confirmed_utc, 1, {_PREFIX[bucket]}) AS k, COUNT(*) AS n "
                f"FROM incidents{where} GROUP BY k",
                params,
            )
            tally = {str(r["k"]): int(r["n"]) for r in grouped}
        return tuple(Count(key=key, count=tally.get(key, 0)) for key in bucket_keys(window, bucket))

    # ---------------------------------------------------------------- durations

    def _durations(
        self, expression: str, window: Window, filters: IncidentFilter, *, extra: str = ""
    ) -> DurationStat:
        where, params = self._where(window, filters)
        joined = (
            f"{where} AND {extra}" if where and extra else (f" WHERE {extra}" if extra else where)
        )
        rows = self._db.query(f"SELECT {expression} AS ms FROM incidents{joined}", params)
        values = [float(r["ms"]) for r in rows if r["ms"] is not None]
        return _duration_stat(values)

    def duration(self, window: Window, filters: IncidentFilter = ALL_INCIDENTS) -> DurationStat:
        return self._durations("duration_ms", window, filters, extra="duration_ms IS NOT NULL")

    def detection_latency(
        self, window: Window, filters: IncidentFilter = ALL_INCIDENTS
    ) -> DurationStat:
        """`confirmed - first_observed`: the headline quality metric of the temporal engine.

        Computed with SQLite's julianday rather than stored, because it is a property of two
        columns that are already there and storing it would let the two disagree.
        """
        return self._durations(
            "(julianday(confirmed_utc) - julianday(first_observed_utc)) * 86400000.0",
            window,
            filters,
        )

    def acknowledgement_time(
        self, window: Window, filters: IncidentFilter = ALL_INCIDENTS
    ) -> DurationStat:
        return self._durations(
            "(julianday(acknowledged_utc) - julianday(confirmed_utc)) * 86400000.0",
            window,
            filters,
            extra="acknowledged_utc IS NOT NULL",
        )

    def resolution_time(
        self, window: Window, filters: IncidentFilter = ALL_INCIDENTS
    ) -> DurationStat:
        return self._durations(
            "(julianday(resolved_utc) - julianday(confirmed_utc)) * 86400000.0",
            window,
            filters,
            extra="resolved_utc IS NOT NULL",
        )

    # ---------------------------------------------------------------- outcomes

    def dismissal_rate(
        self, window: Window, filters: IncidentFilter = ALL_INCIDENTS
    ) -> tuple[tuple[str, str, int, int, float], ...]:
        """(module_id, module_version, dismissed, total, ratio), per module version.

        Per *version* because that is the only way the number answers a question worth asking:
        "did the change we made to this module reduce how often an operator calls it wrong"
        (06 §9). A rate pooled across versions cannot.
        """
        where, params = self._where(window, filters)
        rows = self._db.query(
            "SELECT module_id, module_version, "
            "SUM(CASE WHEN status = 'dismissed' THEN 1 ELSE 0 END) AS dismissed, "
            f"COUNT(*) AS total FROM incidents{where} "
            "GROUP BY module_id, module_version ORDER BY module_id, module_version",
            params,
        )
        return tuple(
            (
                str(r["module_id"]),
                str(r["module_version"]),
                int(r["dismissed"]),
                int(r["total"]),
                (int(r["dismissed"]) / int(r["total"])) if int(r["total"]) else 0.0,
            )
            for r in rows
        )

    def candidate_outcomes(self, window: Window) -> tuple[Count, ...]:
        """A different population from incidents: most of these never became one (06 §1)."""
        rows = self._db.query(
            "SELECT outcome AS k, COUNT(*) AS n FROM candidate_events "
            "WHERE promoted_utc >= ? AND promoted_utc < ? GROUP BY outcome ORDER BY n DESC, k",
            (to_utc_text(window.since), to_utc_text(window.until)),
        )
        return tuple(Count(key=str(r["k"]), count=int(r["n"])) for r in rows)

    def rejection_reasons(self, window: Window, *, limit: int = 20) -> tuple[Count, ...]:
        rows = self._db.query(
            "SELECT rejection_reason AS k, COUNT(*) AS n FROM candidate_events "
            "WHERE outcome = ? AND rejection_reason IS NOT NULL "
            "AND promoted_utc >= ? AND promoted_utc < ? "
            "GROUP BY k ORDER BY n DESC, k LIMIT ?",
            (
                CandidateOutcome.REJECTED.value,
                to_utc_text(window.since),
                to_utc_text(window.until),
                limit,
            ),
        )
        return tuple(Count(key=str(r["k"]), count=int(r["n"])) for r in rows)

    # ---------------------------------------------------------------- cameras

    def camera_availability(
        self, window: Window, *, camera_ids: Sequence[str] = ()
    ) -> dict[str, dict[str, float] | None]:
        """Seconds in each state per camera, or `None` where nothing was recorded.

        `None` rather than an empty mapping, and never 100%: a camera with no recorded health
        transitions has *unknown* availability. Reporting it as perfect is the most dangerous
        wrong number this layer could produce, because a blind camera would look healthy.
        """
        from vigil.persistence.repositories.events import HealthEventRepository

        repository = HealthEventRepository(self._db)
        wanted = list(camera_ids)
        if not wanted:
            rows = self._db.query(
                "SELECT DISTINCT camera_id FROM camera_health_events ORDER BY camera_id"
            )
            wanted = [str(r["camera_id"]) for r in rows]
        out: dict[str, dict[str, float] | None] = {}
        for camera_id in wanted:
            totals = repository.duration_by_state(
                camera_id, since_wall_utc=window.since, until_wall_utc=window.until
            )
            out[camera_id] = (
                None if not totals else {state.value: round(s, 3) for state, s in totals.items()}
            )
        return out

    # ---------------------------------------------------------------- coverage

    def earliest_incident(self) -> datetime | None:
        """What the data actually covers, so a window wider than the history says so."""
        value = self._db.scalar("SELECT MIN(confirmed_utc) FROM incidents")
        return None if value is None else from_utc_text(str(value))

    def open_incidents(self) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM incidents WHERE status IN "
                "('candidate', 'confirmed', 'active', 'resolving')"
            )
            or 0
        )

    def statuses_present(self) -> tuple[IncidentStatus, ...]:
        rows = self._db.query("SELECT DISTINCT status FROM incidents ORDER BY status")
        return tuple(IncidentStatus(r["status"]) for r in rows)

    def event_types_present(self) -> tuple[EventType, ...]:
        rows = self._db.query("SELECT DISTINCT event_type FROM incidents ORDER BY event_type")
        return tuple(EventType(r["event_type"]) for r in rows)
