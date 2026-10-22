"""Alert channels and the response worker: availability, failure, backpressure and spill.

The theme is the same throughout: a thing that cannot work says so, and a thing that fails is
counted rather than swallowed. An alert channel that accepted a send and dropped it would be the
worst fake in the system, because the operator would believe someone had been told.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vigil.config.loader import load_settings
from vigil.config.schema.alerts import AlertsConfig
from vigil.config.schema.storage import StorageConfig
from vigil.core.clock import ManualClock
from vigil.core.errors import PersistenceError
from vigil.core.ids import UlidFactory
from vigil.core.rng import seeded_rng
from vigil.domain.alert import Alert
from vigil.domain.enums import CameraState, Severity
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.database import Database
from vigil.persistence.repositories.events import HealthEventRepository
from vigil.persistence.repositories.metrics import MetricRepository, MetricSample
from vigil.pipeline.analysis import AnalysisTick
from vigil.pipeline.queues import RoundRobinDropQueue, SpillQueue
from vigil.response.channels.base import delivery_failed, delivery_ok
from vigil.response.channels.console import ConsoleChannel
from vigil.response.channels.jsonl import JsonlChannel
from vigil.response.channels.registry import AlertDispatcher, build_channels
from vigil.response.channels.webhook import WebhookChannel

WALL = datetime(2026, 3, 1, 2, 41, tzinfo=UTC)
INCIDENT = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _alert(severity: Severity = Severity.HIGH) -> Alert:
    return Alert(
        alert_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        incident_id=INCIDENT,
        severity=severity,
        created_wall_utc=WALL,
        channels=("console",),
        deliveries=(),
    )


def _ulids() -> UlidFactory:
    return UlidFactory(ManualClock(wall_start=WALL), seeded_rng(3, "alerts"))


# ---------------------------------------------------------------- helpers


def test_a_successful_delivery_cannot_carry_an_error() -> None:
    assert delivery_ok("console").error is None
    failed = delivery_failed("console", "boom", attempts=3)
    assert failed.attempts == 3 and not failed.ok


# ---------------------------------------------------------------- console


def test_the_console_channel_is_always_available() -> None:
    channel = ConsoleChannel(get_logger("test.alerts"))
    assert channel.is_available()
    assert channel.unavailable_reason() is None
    assert channel.send(_alert()).ok


# ---------------------------------------------------------------- jsonl


def test_the_jsonl_channel_appends_one_object_per_alert(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "alerts.jsonl"
    channel = JsonlChannel(path, logger=get_logger("test.alerts"))
    assert channel.is_available(), "availability is tested by opening the file, not guessed"
    assert channel.send(_alert()).ok
    assert channel.send(_alert(Severity.CRITICAL)).ok

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["incident_id"] == INCIDENT
    assert first["severity"] == "HIGH"
    assert json.loads(lines[1])["severity"] == "CRITICAL"


def test_an_unwritable_jsonl_path_reports_unavailable_with_a_reason(tmp_path: Path) -> None:
    # A directory where the file should be: opening it for append fails.
    path = tmp_path / "alerts.jsonl"
    path.mkdir()
    channel = JsonlChannel(path, logger=get_logger("test.alerts"))
    assert not channel.is_available()
    assert "cannot append" in (channel.unavailable_reason() or "")
    delivery = channel.send(_alert())
    assert not delivery.ok
    assert delivery.error


# ---------------------------------------------------------------- webhook


def test_an_unconfigured_webhook_is_unavailable_and_never_sends() -> None:
    channel = WebhookChannel(url="", timeout_ms=100, max_attempts=3, logger=get_logger("t"))
    assert not channel.is_available()
    assert "webhook_url" in (channel.unavailable_reason() or "")
    delivery = channel.send(_alert())
    assert not delivery.ok
    assert delivery.attempts == 0, "nothing was attempted"


def test_a_non_http_webhook_scheme_is_refused() -> None:
    channel = WebhookChannel(
        url="file:///etc/passwd", timeout_ms=100, max_attempts=1, logger=get_logger("t")
    )
    assert not channel.is_available()
    assert "http" in (channel.unavailable_reason() or "")


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_a_webhook_posts_the_incident_identity_and_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, bytes, dict[str, str]]] = []

    def fake_urlopen(request: object, timeout: float = 0.0) -> _Response:
        assert isinstance(request, urllib.request.Request)
        sent.append((request.full_url, request.data or b"", dict(request.headers)))
        return _Response(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    channel = WebhookChannel(
        url="https://example.invalid/hook",
        timeout_ms=500,
        max_attempts=3,
        logger=get_logger("t"),
    )
    delivery = channel.send(_alert())
    assert delivery.ok and delivery.attempts == 1

    url, body, headers = sent[0]
    assert url == "https://example.invalid/hook"
    payload = json.loads(body)
    # No evidence, no explanation, no pixels: identity and severity only.
    assert set(payload) == {"alert_id", "incident_id", "severity", "created_wall_utc"}
    assert headers["Content-type"] == "application/json"


def test_a_server_error_is_retried_up_to_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def fake_urlopen(_request: object, timeout: float = 0.0) -> _Response:
        attempts.append(1)
        return _Response(503)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    channel = WebhookChannel(
        url="http://example.invalid/hook", timeout_ms=10, max_attempts=3, logger=get_logger("t")
    )
    delivery = channel.send(_alert())
    assert not delivery.ok
    assert delivery.attempts == 3
    assert len(attempts) == 3
    assert "503" in (delivery.error or "")


def test_a_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx is the receiver saying "do not retry"; hammering it would be rude and useless."""
    attempts: list[int] = []

    def fake_urlopen(_request: object, timeout: float = 0.0) -> _Response:
        attempts.append(1)
        raise urllib.error.HTTPError("u", 404, "not found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    channel = WebhookChannel(
        url="http://example.invalid/hook", timeout_ms=10, max_attempts=4, logger=get_logger("t")
    )
    delivery = channel.send(_alert())
    assert not delivery.ok
    assert len(attempts) == 1, "a 404 is final"


def test_a_network_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_request: object, timeout: float = 0.0) -> _Response:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    channel = WebhookChannel(
        url="http://example.invalid/hook", timeout_ms=10, max_attempts=2, logger=get_logger("t")
    )
    delivery = channel.send(_alert())
    assert not delivery.ok
    assert "URLError" in (delivery.error or "")


def test_the_config_refuses_a_webhook_channel_with_no_url() -> None:
    with pytest.raises(ValueError, match="cannot deliver"):
        AlertsConfig(channels=("webhook",))


def test_the_config_refuses_credentials_in_a_webhook_url() -> None:
    with pytest.raises(ValueError, match="credentials"):
        AlertsConfig(webhook_url="https://user:secret@example.invalid/hook")


# ---------------------------------------------------------------- the dispatcher


def _dispatcher(config: AlertsConfig, *, alerts_file: Path) -> AlertDispatcher:
    return AlertDispatcher(
        channels=build_channels(config, logger=get_logger("t"), alerts_file=alerts_file),
        config=config,
        clock=ManualClock(wall_start=WALL),
        ulids=_ulids(),
        logger=get_logger("t"),
        metrics=MetricsRegistry(),
    )


def test_only_the_listed_channels_are_built(tmp_path: Path) -> None:
    dispatcher = _dispatcher(
        AlertsConfig(channels=("console", "jsonl")), alerts_file=tmp_path / "a.jsonl"
    )
    assert dispatcher.channel_ids == ("console", "jsonl")
    assert {s.channel_id for s in dispatcher.status()} == {"console", "jsonl"}


def test_an_alert_below_the_threshold_is_not_dispatched_and_says_why(tmp_path: Path) -> None:
    dispatcher = _dispatcher(
        AlertsConfig(channels=("console",), min_severity="HIGH"), alerts_file=tmp_path / "a.jsonl"
    )
    result = dispatcher.dispatch(INCIDENT, Severity.LOW)
    assert not result.dispatched
    assert "below alerts.min_severity" in (result.suppressed_reason or "")

    allowed = dispatcher.dispatch(INCIDENT, Severity.CRITICAL)
    assert allowed.dispatched
    assert allowed.alert is not None
    assert allowed.alert.channels == ("console",)


def test_disabled_alerting_dispatches_nothing(tmp_path: Path) -> None:
    dispatcher = _dispatcher(AlertsConfig(enabled=False), alerts_file=tmp_path / "a.jsonl")
    result = dispatcher.dispatch(INCIDENT, Severity.CRITICAL)
    assert not result.dispatched
    assert "disabled" in (result.suppressed_reason or "")


def test_an_unavailable_channel_is_excluded_but_still_reported(tmp_path: Path) -> None:
    bad = tmp_path / "alerts.jsonl"
    bad.mkdir()
    dispatcher = _dispatcher(AlertsConfig(channels=("console", "jsonl")), alerts_file=bad)
    result = dispatcher.dispatch(INCIDENT, Severity.CRITICAL)
    assert result.dispatched
    assert result.alert is not None
    assert result.alert.channels == ("console",), "the broken channel is not used"
    unavailable = {s.channel_id: s.reason for s in dispatcher.status() if not s.available}
    assert "jsonl" in unavailable, "but it is still named, with its reason"


def test_a_failed_delivery_is_recorded_on_the_alert(tmp_path: Path) -> None:
    class Flaky:
        channel_id = "flaky"

        def is_available(self) -> bool:
            return True

        def unavailable_reason(self) -> str | None:
            return None

        def send(self, alert: Alert) -> object:
            return delivery_failed("flaky", "the receiver refused", attempts=3)

    dispatcher = AlertDispatcher(
        channels=[Flaky()],  # type: ignore[list-item]
        config=AlertsConfig(min_severity="INFO"),
        clock=ManualClock(wall_start=WALL),
        ulids=_ulids(),
        logger=get_logger("t"),
        metrics=MetricsRegistry(),
    )
    result = dispatcher.dispatch(INCIDENT, Severity.HIGH)
    assert result.dispatched
    assert result.alert is not None
    delivery = result.alert.deliveries[0]
    assert not delivery.ok and delivery.attempts == 3
    assert delivery.error == "the receiver refused"


# ---------------------------------------------------------------- the queues


def test_the_analysis_queue_drops_the_oldest_per_camera() -> None:
    queue: RoundRobinDropQueue[str] = RoundRobinDropQueue(2)
    assert queue.put("cam-a", "1") is None
    assert queue.put("cam-a", "2") is None
    report = queue.put("cam-a", "3")
    assert report is not None and report.dropped == 1 and report.camera_id == "cam-a"
    assert queue.depth("cam-a") == 2
    assert queue.take(0.0) == "2", "the oldest went, the recent picture stayed"


def test_a_busy_camera_cannot_evict_a_quiet_one() -> None:
    """A shared bound would make a slow camera look like a failing detector."""
    queue: RoundRobinDropQueue[str] = RoundRobinDropQueue(1)
    queue.put("fast", "f1")
    queue.put("slow", "s1")
    for n in range(10):
        queue.put("fast", f"f{n + 2}")
    assert queue.depth("slow") == 1
    taken = {queue.take(0.0), queue.take(0.0)}
    assert "s1" in taken


def test_the_analysis_queue_drains_in_rotation() -> None:
    queue: RoundRobinDropQueue[str] = RoundRobinDropQueue(4)
    for key in ("a", "b", "c"):
        queue.put(key, f"{key}1")
        queue.put(key, f"{key}2")
    order = [queue.take(0.0) for _ in range(6)]
    # Each camera is served before any is served twice.
    assert {order[0], order[1], order[2]} == {"a1", "b1", "c1"}


def test_a_closed_queue_accepts_nothing_and_wakes_its_consumer() -> None:
    queue: RoundRobinDropQueue[str] = RoundRobinDropQueue(2)
    queue.close()
    assert queue.closed
    assert queue.put("a", "1") is None
    assert queue.take(0.0) is None


def test_forgetting_a_camera_discards_its_queue() -> None:
    queue: RoundRobinDropQueue[str] = RoundRobinDropQueue(2)
    queue.put("a", "1")
    queue.forget("a")
    assert queue.depth() == 0
    assert queue.keys() == ()


def test_the_response_queue_blocks_then_spills() -> None:
    """Losing a confirmed incident is unacceptable, but the analysis thread must not stall."""
    spilled: list[str] = []
    queue: SpillQueue[str] = SpillQueue(2, spill=spilled.append)
    assert queue.put("a", timeout_s=0.0)
    assert queue.put("b", timeout_s=0.0)
    assert not queue.put("c", timeout_s=0.01), "full: the third is spilled"
    assert spilled == ["c"]
    assert queue.spilled_total == 1
    assert queue.depth() == 2


def test_the_response_queue_accepts_again_once_drained() -> None:
    queue: SpillQueue[str] = SpillQueue(1, spill=lambda _x: None)
    queue.put("a", timeout_s=0.0)
    assert queue.take(0.0) == "a"
    assert queue.put("b", timeout_s=0.0)


def test_a_stop_signal_aborts_a_blocked_put() -> None:
    spilled: list[str] = []
    queue: SpillQueue[str] = SpillQueue(1, spill=spilled.append)
    queue.put("a", timeout_s=0.0)
    stop = threading.Event()
    stop.set()
    assert not queue.put("b", timeout_s=5.0, stop=stop)
    assert spilled == ["b"], "the item is spilled rather than lost to shutdown"


def test_draining_returns_everything_in_order() -> None:
    queue: SpillQueue[str] = SpillQueue(5, spill=lambda _x: None)
    for item in "abc":
        queue.put(item, timeout_s=0.0)
    assert queue.drain() == ("a", "b", "c")
    assert queue.depth() == 0


def test_the_spill_sink_is_replaceable() -> None:
    received: list[str] = []
    queue: SpillQueue[str] = SpillQueue(1, spill=lambda _x: None)
    queue.set_spill(received.append)
    queue.put("a", timeout_s=0.0)
    queue.put("b", timeout_s=0.0)
    assert received == ["b"]


# ---------------------------------------------------------------- metrics and health rows


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


def _sample(at: datetime, *, gpu: float | None = 41.0) -> MetricSample:
    return MetricSample(
        t_wall_utc=at,
        cpu_percent=22.5,
        memory_used_mb=8000.0,
        process_rss_mb=420.0,
        gpu_utilization_percent=gpu,
        gpu_memory_used_mb=None if gpu is None else 2100.0,
        gpu_temperature_c=None if gpu is None else 64.0,
        pipeline_fps=14.2,
        inference_latency_p50_ms=28.0,
        inference_latency_p95_ms=62.0,
        queue_depths={"analysis:cam": 1},
        frames_dropped_total=3,
    )


def test_metrics_are_written_in_one_batch_and_read_back(store: Database) -> None:
    repo = MetricRepository(store)
    samples = [_sample(WALL + timedelta(seconds=n)) for n in range(5)]
    assert repo.write_batch(samples) == 5
    assert repo.count() == 5

    series = repo.series(since_wall_utc=WALL, until_wall_utc=WALL + timedelta(minutes=1), limit=10)
    assert len(series) == 5
    assert series[0].queue_depths == {"analysis:cam": 1}
    assert series[0].pipeline_fps == pytest.approx(14.2)


def test_an_unmeasured_gpu_round_trips_as_none_never_zero(store: Database) -> None:
    """The UI must render "unavailable", not 0 (05 §4)."""
    repo = MetricRepository(store)
    repo.write_batch([_sample(WALL, gpu=None)])
    sample = repo.series(since_wall_utc=WALL, until_wall_utc=WALL + timedelta(minutes=1), limit=1)[
        0
    ]
    assert sample.gpu_utilization_percent is None
    assert sample.gpu_memory_used_mb is None


def test_writing_an_empty_batch_is_a_no_op(store: Database) -> None:
    assert MetricRepository(store).write_batch([]) == 0


def test_metrics_are_downsampled_to_hourly_before_deletion(store: Database) -> None:
    """A month-old trend survives at a fraction of the rows rather than vanishing (05 §8)."""
    repo = MetricRepository(store)
    old = WALL - timedelta(days=30)
    repo.write_batch([_sample(old + timedelta(minutes=n)) for n in range(5)])
    repo.write_batch([_sample(WALL)])
    assert repo.count() == 6

    hours, deleted = repo.downsample_and_delete_older_than(WALL - timedelta(days=1))
    assert hours == 1
    assert deleted >= 4
    remaining = repo.count()
    assert remaining < 6
    # The hourly aggregate is still there, and so is the recent sample.
    assert remaining >= 2


def test_health_events_are_recorded_and_read_newest_first(store: Database) -> None:
    repo = HealthEventRepository(store)
    repo.record("cam-a", CameraState.ONLINE, WALL, None)
    repo.record("cam-a", CameraState.RECONNECTING, WALL + timedelta(seconds=30), "read timeout")
    repo.record("cam-b", CameraState.ONLINE, WALL + timedelta(seconds=40), None)

    recent = repo.recent(limit=10)
    assert recent[0].camera_id == "cam-b"
    only_a = repo.recent(limit=10, camera_id="cam-a")
    assert {e.camera_id for e in only_a} == {"cam-a"}
    assert only_a[0].detail == "read timeout"


def test_time_in_each_state_is_computed_from_the_transitions(store: Database) -> None:
    """Computed from what happened, not sampled: this is what camera reliability is built on."""
    repo = HealthEventRepository(store)
    repo.record("cam-a", CameraState.ONLINE, WALL, None)
    repo.record("cam-a", CameraState.RECONNECTING, WALL + timedelta(seconds=60), "lost")
    repo.record("cam-a", CameraState.ONLINE, WALL + timedelta(seconds=90), None)

    totals = repo.duration_by_state(
        "cam-a", since_wall_utc=WALL, until_wall_utc=WALL + timedelta(seconds=120)
    )
    assert totals[CameraState.ONLINE] == pytest.approx(90.0)
    assert totals[CameraState.RECONNECTING] == pytest.approx(30.0)


def test_no_history_means_no_durations_rather_than_zeroes(store: Database) -> None:
    repo = HealthEventRepository(store)
    assert (
        repo.duration_by_state(
            "cam-a", since_wall_utc=WALL, until_wall_utc=WALL + timedelta(hours=1)
        )
        == {}
    )


def test_old_health_events_are_deleted(store: Database) -> None:
    repo = HealthEventRepository(store)
    repo.record("cam-a", CameraState.ONLINE, WALL - timedelta(days=60), None)
    repo.record("cam-a", CameraState.ONLINE, WALL, None)
    assert repo.delete_older_than(WALL - timedelta(days=30), limit=100) == 1
    assert len(repo.recent(limit=10)) == 1


# ---------------------------------------------------------------- the response worker


def test_a_spilled_tick_is_counted_and_logged() -> None:
    """The spill path runs on the analysis thread, so it must not touch the database."""
    from vigil.pipeline.response_worker import ResponseWorker

    metrics = MetricsRegistry()
    queue: SpillQueue[AnalysisTick] = SpillQueue(1, spill=lambda _t: None)
    worker = ResponseWorker(
        queue=queue,
        incidents=None,  # type: ignore[arg-type]
        candidates=None,  # type: ignore[arg-type]
        evidence=None,  # type: ignore[arg-type]
        capture=None,  # type: ignore[arg-type]
        alerts=None,  # type: ignore[arg-type]
        identity=None,  # type: ignore[arg-type]
        settings=load_settings(env={}, use_dotenv=False, config_dir=Path("config")).settings,
        clock=ManualClock(wall_start=WALL),
        logger=get_logger("test.respond"),
        metrics=metrics,
    )
    worker.spill(AnalysisTick(camera_id="cam-a", t_monotonic_ns=1, wall_utc=WALL))
    assert metrics.counter("vigil_response_spilled_total", camera="cam-a").value == 1


def test_a_tick_with_nothing_in_it_is_not_worth_handing_over() -> None:
    assert not AnalysisTick(camera_id="cam-a", t_monotonic_ns=1).has_work


def test_a_persistence_failure_in_one_step_does_not_lose_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail open: a failed candidate write must not stop the release being recorded."""
    from vigil.pipeline.response_worker import ResponseWorker

    class Breaking:
        def record_all(self, *_a: object, **_k: object) -> int:
            raise PersistenceError("disk went away")

    metrics = MetricsRegistry()
    queue: SpillQueue[AnalysisTick] = SpillQueue(4, spill=lambda _t: None)
    worker = ResponseWorker(
        queue=queue,
        incidents=None,  # type: ignore[arg-type]
        candidates=Breaking(),  # type: ignore[arg-type]
        evidence=None,  # type: ignore[arg-type]
        capture=None,  # type: ignore[arg-type]
        alerts=None,  # type: ignore[arg-type]
        identity=None,  # type: ignore[arg-type]
        settings=load_settings(env={}, use_dotenv=False, config_dir=Path("config")).settings,
        clock=ManualClock(wall_start=WALL),
        logger=get_logger("test.respond"),
        metrics=metrics,
    )
    from tests.unit.response.test_lifecycle import _event  # reuse the builder
    from vigil.domain.enums import CandidateOutcome
    from vigil.domain.event import CandidateRecord

    record = CandidateRecord(
        candidate=_event(candidate_id="01ARZ3NDEKTSV4RRFFQ69G5FAV").candidate,
        outcome=CandidateOutcome.REJECTED,
        verdicts=(),
        accumulator_trace=(),
        rejection_reason="scene_stability",
        decided_monotonic_ns=1,
        decided_wall_utc=WALL,
    )
    tick = AnalysisTick(camera_id="gate-north", t_monotonic_ns=1, records=(record,), wall_utc=WALL)
    worker.handle(tick)  # must not raise
    assert worker.stats.failures == 1
    assert metrics.counter("vigil_response_errors_total", step="candidates").value == 1


def test_an_empty_sequence_of_channels_dispatches_nothing(tmp_path: Path) -> None:
    dispatcher = AlertDispatcher(
        channels=(),
        config=AlertsConfig(channels=(), min_severity="INFO"),
        clock=ManualClock(wall_start=WALL),
        ulids=_ulids(),
        logger=get_logger("t"),
        metrics=MetricsRegistry(),
    )
    result = dispatcher.dispatch(INCIDENT, Severity.CRITICAL)
    assert not result.dispatched
    assert "no alert channel" in (result.suppressed_reason or "")


def test_build_channels_is_deterministic(tmp_path: Path) -> None:
    config = AlertsConfig(channels=("jsonl", "console"))
    built: Sequence[object] = build_channels(
        config, logger=get_logger("t"), alerts_file=tmp_path / "a.jsonl"
    )
    assert [c.channel_id for c in built] == ["jsonl", "console"]  # type: ignore[attr-defined]
