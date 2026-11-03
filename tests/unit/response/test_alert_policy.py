"""Alert rules and quiet hours: they narrow what interrupts people, never what is recorded."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from vigil.config.schema.alerts import AlertRuleConfig, AlertsConfig, QuietHoursConfig
from vigil.config.schema.zones import ScheduleConfig
from vigil.core.clock import ManualClock
from vigil.core.ids import UlidFactory
from vigil.core.rng import seeded_rng
from vigil.domain.enums import Severity
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.response.alert_policy import AlertPolicy
from vigil.response.channels.registry import AlertDispatcher, build_channels

INCIDENT = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOON = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)  # a Monday
NIGHT = datetime(2026, 3, 2, 23, 30, tzinfo=UTC)
SMALL_HOURS = datetime(2026, 3, 3, 2, 0, tzinfo=UTC)  # Tuesday 02:00, inside a window that wrapped

OVERNIGHT = QuietHoursConfig(name="night", schedule=ScheduleConfig(start="22:00", end="06:00"))


def decide(
    config: AlertsConfig,
    severity: Severity,
    *,
    camera: str | None = "gate",
    event: str | None = "intrusion",
    at: datetime = NOON,
):  # type: ignore[no-untyped-def]
    return AlertPolicy(config).decide(
        camera_id=camera, event_type=event, severity=severity, wall_utc=at
    )


# ------------------------------------------------------------------ the default threshold


def test_the_default_threshold_applies_when_nothing_else_does() -> None:
    config = AlertsConfig(min_severity="HIGH")
    assert decide(config, Severity.HIGH).send
    low = decide(config, Severity.MODERATE)
    assert not low.send and "below alerts.min_severity (HIGH)" in (low.reason or "")


def test_disabled_alerting_says_so_before_anything_else() -> None:
    d = decide(AlertsConfig(enabled=False, rules=(AlertRuleConfig(name="a"),)), Severity.CRITICAL)
    assert not d.send and "disabled" in (d.reason or "")


# ------------------------------------------------------------------ rules


def test_a_rule_can_lower_the_threshold_for_one_camera() -> None:
    config = AlertsConfig(
        min_severity="HIGH",
        rules=(AlertRuleConfig(name="door", camera="door", min_severity="LOW"),),
    )
    assert decide(config, Severity.LOW, camera="door").send
    assert not decide(config, Severity.LOW, camera="gate").send


def test_a_rule_can_raise_the_threshold_for_one_event_type() -> None:
    config = AlertsConfig(
        min_severity="LOW",
        rules=(
            AlertRuleConfig(name="loud", event_types=("person_down",), min_severity="CRITICAL"),
        ),
    )
    quiet = decide(config, Severity.HIGH, event="person_down")
    assert not quiet.send and "rule 'loud'" in (quiet.reason or "")
    assert decide(config, Severity.HIGH, event="intrusion").send


def test_the_first_matching_rule_wins() -> None:
    config = AlertsConfig(
        min_severity="CRITICAL",
        rules=(
            AlertRuleConfig(name="first", camera="gate", min_severity="LOW"),
            AlertRuleConfig(name="second", camera="gate", min_severity="CRITICAL"),
        ),
    )
    d = decide(config, Severity.LOW)
    assert d.send and d.rule == "first"


def test_a_rule_can_pick_channels() -> None:
    config = AlertsConfig(
        channels=("console", "jsonl"),
        rules=(AlertRuleConfig(name="r", camera="gate", channels=("jsonl",)),),
    )
    assert decide(config, Severity.HIGH).channels == frozenset({"jsonl"})
    assert decide(config, Severity.HIGH, camera="elsewhere").channels is None


def test_rules_are_skipped_when_the_incident_is_not_described() -> None:
    config = AlertsConfig(
        min_severity="HIGH", rules=(AlertRuleConfig(name="r", min_severity="INFO"),)
    )
    assert not decide(config, Severity.LOW, camera=None, event=None).send


# ------------------------------------------------------------------ quiet hours


def test_quiet_hours_hold_lower_severities_and_let_critical_through() -> None:
    config = AlertsConfig(min_severity="LOW", quiet_hours=(OVERNIGHT,))
    held = decide(config, Severity.HIGH, at=NIGHT)
    assert not held.send and "quiet hours 'night'" in (held.reason or "")
    assert "still recorded" in (held.reason or "")
    assert decide(config, Severity.CRITICAL, at=NIGHT).send
    assert decide(config, Severity.HIGH, at=NOON).send


def test_a_window_that_wraps_midnight_covers_the_small_hours() -> None:
    config = AlertsConfig(min_severity="LOW", quiet_hours=(OVERNIGHT,))
    assert not decide(config, Severity.HIGH, at=SMALL_HOURS).send


def test_a_window_can_let_through_from_a_lower_bar() -> None:
    window = QuietHoursConfig(name="night", schedule=OVERNIGHT.schedule, let_through_from="HIGH")
    config = AlertsConfig(min_severity="LOW", quiet_hours=(window,))
    assert decide(config, Severity.HIGH, at=NIGHT).send
    assert not decide(config, Severity.MODERATE, at=NIGHT).send


def test_a_window_limited_to_weekdays_is_quiet_only_on_them() -> None:
    weekend = QuietHoursConfig(
        name="weekend",
        schedule=ScheduleConfig(start="00:00", end="23:59", weekdays=(5, 6)),
    )
    config = AlertsConfig(min_severity="LOW", quiet_hours=(weekend,))
    saturday = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    assert not decide(config, Severity.HIGH, at=saturday).send
    assert decide(config, Severity.HIGH, at=NOON).send  # a Monday


def test_a_window_respects_its_utc_offset() -> None:
    plus_two = QuietHoursConfig(
        name="local-night",
        schedule=ScheduleConfig(start="22:00", end="06:00", utc_offset_minutes=120),
    )
    config = AlertsConfig(min_severity="LOW", quiet_hours=(plus_two,))
    at_2030_utc = datetime(2026, 3, 2, 20, 30, tzinfo=UTC)  # 22:30 local
    assert not decide(config, Severity.HIGH, at=at_2030_utc).send


def test_the_below_threshold_reason_wins_over_quiet_hours() -> None:
    config = AlertsConfig(min_severity="HIGH", quiet_hours=(OVERNIGHT,))
    d = decide(config, Severity.LOW, at=NIGHT)
    assert "below alerts.min_severity" in (d.reason or "")


def test_held_by_quiet_hours_names_the_window_for_display() -> None:
    policy = AlertPolicy(AlertsConfig(quiet_hours=(OVERNIGHT,)))
    assert policy.held_by_quiet_hours(Severity.HIGH, NIGHT) == "night"
    assert policy.held_by_quiet_hours(Severity.CRITICAL, NIGHT) is None
    assert policy.held_by_quiet_hours(Severity.HIGH, NOON) is None


# ------------------------------------------------------------------ configuration


def test_critical_cannot_be_held_because_there_is_nothing_above_it() -> None:
    assert OVERNIGHT.let_through_from == "CRITICAL"
    with pytest.raises(ValidationError):
        QuietHoursConfig(name="x", schedule=OVERNIGHT.schedule, let_through_from="IMPOSSIBLE")  # type: ignore[arg-type]


def test_a_rule_cannot_name_an_unknown_event_type_or_camera() -> None:
    with pytest.raises(ValidationError, match="unknown event types"):
        AlertRuleConfig(name="r", event_types=("not_a_thing",))
    with pytest.raises(ValidationError, match="not a camera id"):
        AlertRuleConfig(name="r", camera="Not A Camera!")


def test_a_rule_cannot_send_to_a_channel_that_is_not_enabled() -> None:
    with pytest.raises(ValidationError, match=r"not in alerts.channels"):
        AlertsConfig(channels=("console",), rules=(AlertRuleConfig(name="r", channels=("jsonl",)),))


def test_names_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="rule names"):
        AlertsConfig(rules=(AlertRuleConfig(name="a"), AlertRuleConfig(name="a")))
    with pytest.raises(ValidationError, match="quiet-hours names"):
        AlertsConfig(quiet_hours=(OVERNIGHT, OVERNIGHT))


def test_the_number_of_rules_is_bounded() -> None:
    with pytest.raises(ValidationError):
        AlertsConfig(rules=tuple(AlertRuleConfig(name=f"r{i}") for i in range(40)))


# ------------------------------------------------------------------ through the dispatcher


def dispatcher(config: AlertsConfig, tmp_path: Path, at: datetime) -> AlertDispatcher:
    return AlertDispatcher(
        channels=build_channels(config, logger=get_logger("t"), alerts_file=tmp_path / "a.jsonl"),
        config=config,
        clock=ManualClock(wall_start=at),
        ulids=UlidFactory(ManualClock(), seeded_rng(1, "alerts")),
        logger=get_logger("t"),
        metrics=MetricsRegistry(),
    )


def test_a_rule_restricts_delivery_to_its_channels(tmp_path: Path) -> None:
    config = AlertsConfig(
        channels=("console", "jsonl"),
        rules=(AlertRuleConfig(name="r", camera="gate", channels=("console",)),),
    )
    result = dispatcher(config, tmp_path, NOON).dispatch(
        INCIDENT, Severity.HIGH, camera_id="gate", event_type="intrusion"
    )
    assert result.alert is not None and result.alert.channels == ("console",)


def test_a_held_alert_is_counted_and_explained(tmp_path: Path) -> None:
    config = AlertsConfig(min_severity="LOW", quiet_hours=(OVERNIGHT,))
    d = dispatcher(config, tmp_path, NIGHT)
    result = d.dispatch(INCIDENT, Severity.HIGH, camera_id="gate", event_type="intrusion")
    assert not result.dispatched and "quiet hours" in (result.suppressed_reason or "")
    assert d.dispatch(
        INCIDENT, Severity.CRITICAL, camera_id="gate", event_type="intrusion"
    ).dispatched


def test_a_live_reconfigure_changes_the_policy_but_not_the_channels(tmp_path: Path) -> None:
    d = dispatcher(AlertsConfig(min_severity="HIGH"), tmp_path, NOON)
    assert not d.dispatch(INCIDENT, Severity.LOW).dispatched
    d.reconfigure(AlertsConfig(min_severity="LOW"))
    assert d.dispatch(INCIDENT, Severity.LOW).dispatched
    assert d.channel_ids == ("console",)
