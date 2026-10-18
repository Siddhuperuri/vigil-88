"""What each reported number means, and the shapes a measurement comes back in (08 §3).

Separated from `queries.py` because it is vocabulary, not logic: the definitions change when
the *meaning* of a metric changes, while the queries change when the schema does. `Count` and
`DurationStat` live here with them because a result is only interpretable alongside the
definition it answers, and keeping the pair together means neither can be imported without the
other being available.

Every metric the analytics layer reports has an entry here, and the entry travels with the
result to wherever it is displayed or exported. The reason is specific: "incidents", "events"
and "candidates" are three different populations (06 §1), and a figure that silently mixed two
of them would sound meaningful and be wrong. Making the population part of the metric means a
screen can always say what it is counting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """What a number means, carried with it so a screen can explain itself."""

    name: str
    population: str
    definition: str
    unit: str


@dataclass(frozen=True, slots=True)
class Count:
    key: str
    count: int


@dataclass(frozen=True, slots=True)
class DurationStat:
    """`None` everywhere means the population was empty — not that the duration was zero."""

    count: int
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None

    @property
    def available(self) -> bool:
        return self.count > 0


EMPTY_DURATION = DurationStat(0, None, None, None, None)

DEFINITIONS: Mapping[str, MetricDefinition] = {
    "incidents_total": MetricDefinition(
        name="incidents_total",
        population="persisted incidents",
        definition="rows in `incidents` whose confirmed_utc falls in the window and which match "
        "the filter",
        unit="incidents",
    ),
    "by_event_type": MetricDefinition(
        name="by_event_type",
        population="persisted incidents",
        definition="incidents grouped by event_type",
        unit="incidents",
    ),
    "by_severity": MetricDefinition(
        name="by_severity",
        population="persisted incidents",
        definition="incidents grouped by the severity band stored at confirmation",
        unit="incidents",
    ),
    "by_status": MetricDefinition(
        name="by_status",
        population="persisted incidents",
        definition="incidents grouped by their current lifecycle status",
        unit="incidents",
    ),
    "by_camera": MetricDefinition(
        name="by_camera",
        population="persisted incidents",
        definition="incidents grouped by camera_id",
        unit="incidents",
    ),
    "trend": MetricDefinition(
        name="trend",
        population="persisted incidents",
        definition="incidents grouped by UTC time bucket; empty buckets are present with 0",
        unit="incidents per bucket",
    ),
    "duration": MetricDefinition(
        name="duration",
        population="persisted incidents with a recorded duration",
        definition="duration_ms, which is set when an incident reaches a terminal status; open "
        "incidents are excluded rather than counted as zero",
        unit="milliseconds",
    ),
    "detection_latency": MetricDefinition(
        name="detection_latency",
        population="persisted incidents",
        definition="confirmed_utc minus first_observed_utc: how long the temporal engine took "
        "to decide",
        unit="milliseconds",
    ),
    "acknowledgement": MetricDefinition(
        name="acknowledgement",
        population="incidents an operator acknowledged",
        definition="acknowledged_utc minus confirmed_utc, over incidents where an operator "
        "actually acknowledged; the rest are unknown, not instant",
        unit="milliseconds",
    ),
    "resolution": MetricDefinition(
        name="resolution",
        population="incidents that reached a terminal status",
        definition="resolved_utc minus confirmed_utc",
        unit="milliseconds",
    ),
    "dismissal_rate": MetricDefinition(
        name="dismissal_rate",
        population="persisted incidents, grouped by module and version",
        definition="dismissed / total. This is the DISMISSAL rate, not the detector's "
        "false-positive rate: it omits every false positive nobody dismissed and every true "
        "positive nobody acknowledged",
        unit="ratio",
    ),
    "candidate_outcomes": MetricDefinition(
        name="candidate_outcomes",
        population="promoted event candidates",
        definition="rows in `candidate_events` grouped by outcome. A candidate is not an "
        "incident: most rejections never became one",
        unit="candidates",
    ),
    "camera_availability": MetricDefinition(
        name="camera_availability",
        population="recorded camera health transitions",
        definition="seconds in each state, from consecutive `camera_health_events`. A camera "
        "with no recorded transitions has unknown availability, not 100%",
        unit="seconds",
    ),
}
