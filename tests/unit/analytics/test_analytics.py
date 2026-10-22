"""Analytics: do the numbers reconcile, and does an uncomputable one say so.

The headline property is reconciliation — every aggregate is checked against a direct SQL count
over the same rows, because an aggregate that disagrees with the table is worse than no
aggregate. After that, most of these tests are about **absence**: a camera with no health
history, a population with no acknowledgements, a window that predates the data. Each must come
back unavailable rather than zero, because on a dashboard a zero and an absence look identical
and only one of them is true.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.response.test_lifecycle import CONTEXT, _assessment, _event, _ulid
from vigil.analytics.queries import (
    DEFINITIONS,
    AnalyticsQueries,
    Bucket,
    Window,
    bucket_key,
    bucket_keys,
)
from vigil.analytics.reports import ReportBuilder
from vigil.config.loader import load_settings
from vigil.config.schema.storage import StorageConfig
from vigil.core.clock import ManualClock
from vigil.core.ids import UlidFactory
from vigil.core.rng import seeded_rng
from vigil.domain.enums import (
    CameraState,
    CandidateOutcome,
    EventType,
    Severity,
    ValidatorOutcome,
)
from vigil.domain.event import CandidateRecord, ValidatorVerdict
from vigil.domain.observation import SubjectKey
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.database import Database
from vigil.persistence.queries import ALL_INCIDENTS, IncidentFilter
from vigil.persistence.repositories.events import CandidateRepository, HealthEventRepository
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.persistence.repositories.incidents import IncidentRepository
from vigil.response.incident_manager import IncidentManager, dedup_key_text
from vigil.response.operator_actions import OperatorActions

WALL = datetime(2026, 3, 1, 2, 41, tzinfo=UTC)
WINDOW = Window(since=WALL - timedelta(days=7), until=WALL + timedelta(days=1))
CAMERA = "gate-north"


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


def _manager(store: Database, *, seed: int = 9, now: datetime = WALL) -> IncidentManager:
    """`seed` must differ between batches: the ULID factory is deterministic, so two managers
    built with one seed would mint the same incident ids and the idempotent insert would
    silently drop the second batch."""
    return IncidentManager(
        incidents=IncidentRepository(store),
        settings=load_settings(env={}, use_dotenv=False, config_dir=Path("config")).settings,
        clock=ManualClock(wall_start=now),
        ulids=UlidFactory(ManualClock(wall_start=now), seeded_rng(seed, "analytics")),
        logger=get_logger("test.incidents"),
        metrics=MetricsRegistry(),
    )


def _actions(store: Database, manager: IncidentManager) -> OperatorActions:
    return OperatorActions(
        manager=manager,
        incidents=IncidentRepository(store),
        clock=ManualClock(wall_start=WALL + timedelta(minutes=5)),
        logger=get_logger("test.operator"),
        metrics=MetricsRegistry(),
    )


def _seed(
    store: Database,
    *,
    count: int = 1,
    camera_id: str = CAMERA,
    event_type: EventType = EventType.INTRUSION,
    severity: Severity = Severity.HIGH,
    score: float = 61.9,
    at: datetime = WALL,
    spacing: timedelta = timedelta(minutes=1),
    start: int = 1,
) -> list[str]:
    """Create `count` distinct incidents, one per subject so dedup does not merge them."""
    manager = _manager(store, seed=start)
    ids: list[str] = []
    for index in range(count):
        subject = SubjectKey("track", str(start + index))
        outcome = manager.record(
            _event(
                candidate_id=_ulid(start + index),
                at=at + spacing * index,
                subject=subject,
                event_type=event_type,
            ),
            _assessment(score, severity),
            CONTEXT,
        )
        ids.append(outcome.incident_id)
    _ = camera_id  # every seeded incident uses the builder's camera
    return ids


def _analytics(store: Database) -> AnalyticsQueries:
    return AnalyticsQueries(store)


def _direct_count(store: Database, where: str = "", params: tuple[object, ...] = ()) -> int:
    """A deliberately naive SQL count, as the oracle the aggregates are checked against."""
    clause = f" WHERE {where}" if where else ""
    # The `where` text is written by this file, never by a caller, and values are still bound.
    sql = f"SELECT COUNT(*) FROM incidents{clause}"  # noqa: S608 - fixed text, bound values
    return int(store.scalar(sql, params) or 0)


# ---------------------------------------------------------------- reconciliation


def test_the_total_reconciles_with_a_direct_count(store: Database) -> None:
    _seed(store, count=5)
    assert _analytics(store).total(WINDOW) == _direct_count(store)


def test_every_breakdown_sums_to_the_total(store: Database) -> None:
    _seed(store, count=3, event_type=EventType.INTRUSION)
    _seed(store, count=2, event_type=EventType.PERSON_DOWN, start=100)
    analytics = _analytics(store)
    total = analytics.total(WINDOW)
    assert total == 5
    for breakdown in (
        analytics.by_event_type(WINDOW),
        analytics.by_severity(WINDOW),
        analytics.by_status(WINDOW),
        analytics.by_camera(WINDOW),
    ):
        assert sum(c.count for c in breakdown) == total


def test_a_breakdown_reconciles_per_key(store: Database) -> None:
    _seed(store, count=3, event_type=EventType.INTRUSION)
    _seed(store, count=2, event_type=EventType.PERSON_DOWN, start=100)
    for count in _analytics(store).by_event_type(WINDOW):
        assert count.count == _direct_count(store, "event_type = ?", (count.key,))


def test_a_filter_narrows_the_aggregate_and_the_total_together(store: Database) -> None:
    _seed(store, count=3, event_type=EventType.INTRUSION)
    _seed(store, count=2, event_type=EventType.PERSON_DOWN, start=100)
    analytics = _analytics(store)
    filters = IncidentFilter(event_types=(EventType.PERSON_DOWN,))
    assert analytics.total(WINDOW, filters) == 2
    assert sum(c.count for c in analytics.by_camera(WINDOW, filters)) == 2


def test_severity_is_reported_by_band_name_not_by_number(store: Database) -> None:
    _seed(store, count=2, severity=Severity.CRITICAL)
    assert [c.key for c in _analytics(store).by_severity(WINDOW)] == ["CRITICAL"]


# ---------------------------------------------------------------- empty data


def test_an_empty_store_reports_zero_and_unavailable_not_an_error(store: Database) -> None:
    analytics = _analytics(store)
    assert analytics.total(WINDOW) == 0
    assert analytics.by_event_type(WINDOW) == ()
    assert analytics.earliest_incident() is None
    for stat in (
        analytics.duration(WINDOW),
        analytics.detection_latency(WINDOW),
        analytics.acknowledgement_time(WINDOW),
        analytics.resolution_time(WINDOW),
    ):
        assert not stat.available
        assert stat.count == 0
        assert stat.mean_ms is None
        assert stat.p95_ms is None


def test_an_unacknowledged_population_has_unknown_acknowledgement_time(
    store: Database,
) -> None:
    """Unknown, not instant: nobody acknowledged these."""
    _seed(store, count=3)
    stat = _analytics(store).acknowledgement_time(WINDOW)
    assert not stat.available
    assert stat.mean_ms is None


def test_an_open_incident_has_no_duration_and_is_not_counted_as_zero(
    store: Database,
) -> None:
    _seed(store, count=2)
    stat = _analytics(store).duration(WINDOW)
    assert stat.count == 0, "duration is set when an incident closes, not before"
    assert not stat.available


def test_acknowledgement_time_is_measured_once_someone_acknowledges(
    store: Database,
) -> None:
    [incident_id] = _seed(store, count=1)
    # A transition is stamped by the manager's clock, not the actions wrapper's: one clock owns
    # the lifecycle so two timestamps on one incident cannot disagree.
    later = _manager(store, seed=500, now=WALL + timedelta(minutes=5))
    _actions(store, later).acknowledge(incident_id)
    stat = _analytics(store).acknowledgement_time(WINDOW)
    assert stat.available
    assert stat.count == 1
    assert stat.mean_ms is not None
    assert stat.mean_ms == pytest.approx(5 * 60 * 1000.0, rel=0.01)


def test_resolution_time_is_measured_once_an_incident_closes(store: Database) -> None:
    [incident_id] = _seed(store, count=1)
    later = _manager(store, seed=501, now=WALL + timedelta(minutes=9))
    _actions(store, later).dismiss(incident_id, reason="false positive")
    stat = _analytics(store).resolution_time(WINDOW)
    assert stat.available and stat.count == 1


def test_detection_latency_comes_from_the_two_stored_timestamps(store: Database) -> None:
    _seed(store, count=1)
    stat = _analytics(store).detection_latency(WINDOW)
    assert stat.available
    assert stat.mean_ms == pytest.approx(900.0, abs=5.0)


# ---------------------------------------------------------------- time


def test_buckets_include_the_empty_ones(store: Database) -> None:
    """A trend drawn only from buckets with rows implies continuity where there was none."""
    _seed(store, count=1, at=WALL)
    trend = _analytics(store).trend(WINDOW, Bucket.DAY)
    assert len(trend) >= 7
    assert sum(c.count for c in trend) == 1
    assert any(c.count == 0 for c in trend), "quiet days are present and zero"


def test_a_trend_reconciles_with_the_total(store: Database) -> None:
    _seed(store, count=4, at=WALL - timedelta(days=2), spacing=timedelta(hours=6))
    analytics = _analytics(store)
    for bucket in Bucket:
        assert sum(c.count for c in analytics.trend(WINDOW, bucket)) == analytics.total(WINDOW)


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        (Bucket.HOUR, "2026-03-01T02"),
        (Bucket.DAY, "2026-03-01"),
        (Bucket.MONTH, "2026-03"),
        (Bucket.WEEK, "2026-02-23"),  # the Monday of that week
    ],
)
def test_bucket_keys_are_stable_and_utc(bucket: Bucket, expected: str) -> None:
    assert bucket_key(WALL, bucket) == expected


def test_a_local_offset_shifts_the_boundary_and_is_recorded() -> None:
    """02:41 UTC is the previous day in a negative offset, and the window says which offset."""
    assert bucket_key(WALL, Bucket.DAY, utc_offset_minutes=0) == "2026-03-01"
    assert bucket_key(WALL, Bucket.DAY, utc_offset_minutes=-4 * 60) == "2026-02-28"
    window = Window(since=WALL, until=WALL + timedelta(days=1), utc_offset_minutes=-240)
    assert window.utc_offset_minutes == -240


def test_a_local_offset_changes_which_bucket_an_incident_lands_in(store: Database) -> None:
    _seed(store, count=1, at=WALL)  # 02:41 UTC
    analytics = _analytics(store)
    utc = Window(since=WALL - timedelta(days=2), until=WALL + timedelta(days=2))
    shifted = Window(
        since=WALL - timedelta(days=2),
        until=WALL + timedelta(days=2),
        utc_offset_minutes=-4 * 60,
    )
    utc_day = next(c.key for c in analytics.trend(utc, Bucket.DAY) if c.count)
    local_day = next(c.key for c in analytics.trend(shifted, Bucket.DAY) if c.count)
    assert utc_day == "2026-03-01"
    assert local_day == "2026-02-28"


def test_a_window_must_end_after_it_starts() -> None:
    with pytest.raises(ValueError, match="end after it starts"):
        Window(since=WALL, until=WALL)


def test_bucket_enumeration_is_bounded() -> None:
    """A five-year hourly window must not try to build 43,000 buckets."""
    wide = Window(since=WALL - timedelta(days=365 * 5), until=WALL)
    assert len(bucket_keys(wide, Bucket.HOUR)) <= 1000


def test_an_incident_outside_the_window_is_excluded(store: Database) -> None:
    _seed(store, count=1, at=WALL - timedelta(days=30))
    narrow = Window(since=WALL - timedelta(days=1), until=WALL + timedelta(days=1))
    assert _analytics(store).total(narrow) == 0
    assert _analytics(store).total(WINDOW) == 0  # also outside the 7-day window


def test_the_window_boundary_is_closed_open(store: Database) -> None:
    """`since` is included and `until` is not, so adjacent windows neither gap nor double-count."""
    _seed(store, count=1, at=WALL)
    on_start = Window(since=WALL, until=WALL + timedelta(hours=1))
    on_end = Window(since=WALL - timedelta(hours=1), until=WALL)
    assert _analytics(store).total(on_start) == 1
    assert _analytics(store).total(on_end) == 0


# ---------------------------------------------------------------- outcomes


def test_the_dismissal_rate_is_per_module_version(store: Database) -> None:
    ids = _seed(store, count=4)
    actions = _actions(store, _manager(store, seed=502))
    actions.dismiss(ids[0], reason="false positive")
    rates = _analytics(store).dismissal_rate(WINDOW)
    assert len(rates) == 1
    module_id, version, dismissed, total, ratio = rates[0]
    assert (module_id, version) == ("intrusion", "1.0.0")
    assert (dismissed, total) == (1, 4)
    assert ratio == pytest.approx(0.25)


def test_the_dismissal_definition_refuses_to_call_itself_a_false_positive_rate() -> None:
    """06 §9 calls dismissal the only honest *measure*; the wording has to keep it that honest."""
    text = DEFINITIONS["dismissal_rate"].definition.lower()
    assert "not the detector's false-positive rate" in text
    assert "omits" in text


def test_candidates_are_a_different_population_from_incidents(store: Database) -> None:
    """Most rejections never became an incident, and conflating the two produces a number that
    sounds meaningful and is not (06 §1)."""
    _seed(store, count=1)
    repo = CandidateRepository(store)
    rejected = CandidateRecord(
        candidate=_event(candidate_id=_ulid(50), subject=SubjectKey("track", "77")).candidate,
        outcome=CandidateOutcome.REJECTED,
        verdicts=(
            ValidatorVerdict("scene_stability", ValidatorOutcome.REJECT, "the view is changing"),
        ),
        accumulator_trace=(),
        rejection_reason="scene_stability: the view is changing",
        decided_monotonic_ns=1,
        decided_wall_utc=WALL,
    )
    repo.record_all([rejected])
    analytics = _analytics(store)
    outcomes = {c.key: c.count for c in analytics.candidate_outcomes(WINDOW)}
    assert outcomes["rejected"] == 1
    assert analytics.total(WINDOW) == 1, "the incident count is unchanged by a rejection"
    reasons = analytics.rejection_reasons(WINDOW)
    assert reasons[0].key == "scene_stability: the view is changing"


# ---------------------------------------------------------------- cameras


def test_a_camera_with_no_health_history_has_unknown_availability(store: Database) -> None:
    """The most dangerous wrong number this layer could produce: a blind camera looking healthy."""
    HealthEventRepository(store).record("other-cam", CameraState.ONLINE, WALL, None)
    availability = _analytics(store).camera_availability(WINDOW, camera_ids=["never-seen"])
    assert availability == {"never-seen": None}


def test_camera_availability_is_computed_from_recorded_transitions(store: Database) -> None:
    repo = HealthEventRepository(store)
    repo.record(CAMERA, CameraState.ONLINE, WALL, None)
    repo.record(CAMERA, CameraState.RECONNECTING, WALL + timedelta(seconds=60), "lost")
    repo.record(CAMERA, CameraState.ONLINE, WALL + timedelta(seconds=90), None)
    window = Window(since=WALL, until=WALL + timedelta(seconds=120))
    totals = _analytics(store).camera_availability(window, camera_ids=[CAMERA])[CAMERA]
    assert totals is not None
    assert totals["online"] == pytest.approx(90.0)
    assert totals["reconnecting"] == pytest.approx(30.0)


# ---------------------------------------------------------------- reports


def _builder(store: Database) -> ReportBuilder:
    return ReportBuilder(
        analytics=_analytics(store),
        incidents=IncidentRepository(store),
        evidence=EvidenceRepository(store),
    )


def test_a_report_states_its_window_filter_and_coverage(store: Database) -> None:
    _seed(store, count=2)
    report = _builder(store).build(
        WINDOW, filters=ALL_INCIDENTS, generated_wall_utc=WALL + timedelta(minutes=1)
    )
    assert report.window == WINDOW
    assert report.filters == ALL_INCIDENTS
    assert report.generated_wall_utc == WALL + timedelta(minutes=1)
    assert report.summary.incidents_total == 2
    assert len(report.incidents) == 2
    assert not report.truncated


def test_a_report_s_summary_cannot_disagree_with_its_rows(store: Database) -> None:
    _seed(store, count=3, event_type=EventType.INTRUSION)
    _seed(store, count=2, event_type=EventType.PERSON_DOWN, start=100)
    filters = IncidentFilter(event_types=(EventType.INTRUSION,))
    report = _builder(store).build(WINDOW, filters=filters, generated_wall_utc=WALL)
    assert report.summary.incidents_total == 3
    assert len(report.incidents) == 3
    assert {r.incident.event_type for r in report.incidents} == {EventType.INTRUSION}


def test_a_truncated_report_says_so(store: Database) -> None:
    _seed(store, count=5)
    report = _builder(store).build(WINDOW, row_limit=2, generated_wall_utc=WALL)
    assert report.truncated
    assert len(report.incidents) == 2
    assert report.summary.incidents_total == 5
    assert any("first page" in c for c in report.caveats)


def test_a_window_predating_the_data_is_declared(store: Database) -> None:
    _seed(store, count=1, at=WALL)
    wide = Window(since=WALL - timedelta(days=365), until=WALL + timedelta(days=1))
    report = _builder(store).build(wide, generated_wall_utc=WALL)
    assert not report.covers_full_window
    assert report.earliest_record is not None
    assert any("predates the data" in c for c in report.caveats)


def test_an_empty_report_is_a_result_not_a_failure(store: Database) -> None:
    report = _builder(store).build(WINDOW, generated_wall_utc=WALL)
    assert report.is_empty
    assert report.incidents == ()
    assert any("fact about the window" in c for c in report.caveats)


def test_a_report_always_says_confidence_is_not_calibrated(store: Database) -> None:
    """Presenting an accumulated log-odds as a probability would be the quietest lie here."""
    _seed(store, count=1)
    report = _builder(store).build(WINDOW, generated_wall_utc=WALL)
    assert any("not probabilities" in c for c in report.caveats)


def test_every_reported_metric_has_a_definition(store: Database) -> None:
    """A number on a screen has to be traceable to the population it came from."""
    _seed(store, count=1)
    report = _builder(store).build(WINDOW, generated_wall_utc=WALL)
    for name in (
        "incidents_total",
        "by_event_type",
        "by_severity",
        "trend",
        "duration",
        "detection_latency",
        "acknowledgement",
        "resolution",
        "dismissal_rate",
        "candidate_outcomes",
        "camera_availability",
    ):
        assert name in report.definitions
        assert report.definitions[name].population
        assert report.definitions[name].definition


def test_a_report_references_evidence_rather_than_embedding_it(store: Database) -> None:
    _seed(store, count=1)
    report = _builder(store).build(WINDOW, generated_wall_utc=WALL)
    row = report.incidents[0]
    assert row.evidence_ids == ()
    assert row.evidence_status == "pending"


def test_the_summary_only_report_skips_the_rows(store: Database) -> None:
    _seed(store, count=3)
    report = _builder(store).build(WINDOW, generated_wall_utc=WALL, include_incidents=False)
    assert report.summary.incidents_total == 3
    assert report.incidents == ()
    assert not report.truncated


def test_dedup_means_one_situation_is_one_row(store: Database) -> None:
    """Analytics counts incidents, and repeated evidence for one situation is one incident."""
    manager = _manager(store, seed=600)
    for index in range(5):
        manager.record(
            _event(candidate_id=_ulid(index + 1), at=WALL + timedelta(seconds=index)),
            _assessment(),
            CONTEXT,
        )
    assert _analytics(store).total(WINDOW) == 1
    assert _direct_count(store) == 1
    assert dedup_key_text(CAMERA, EventType.INTRUSION, SubjectKey("track", "42")) in str(
        store.scalar("SELECT dedup_key FROM incidents")
    )
