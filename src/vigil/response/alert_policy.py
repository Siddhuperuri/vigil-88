"""Who is interrupted, and when: alert rules and quiet hours (06 §10, plan H).

The policy is a pure function of the incident, its severity and the wall clock, so every branch is
tested without a channel or a database. It decides only whether, and where, to *dispatch*. It
never decides whether an incident is *recorded*: the response worker persists the incident before
it asks the policy anything.

Order of decisions, each stated in the reason that comes back:

  1. alerting disabled                      -> not sent
  2. first matching rule                    -> its threshold and channels replace the default
  3. below the (rule or default) threshold  -> not sent
  4. inside a quiet window, below its bar   -> held; CRITICAL always passes
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vigil.config.schema.alerts import AlertRuleConfig, AlertsConfig, QuietHoursConfig
from vigil.config.schema.zones import parse_clock
from vigil.domain.enums import Severity
from vigil.domain.scene import Schedule


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    send: bool
    reason: str | None  # why it was not sent; None when it was
    channels: frozenset[str] | None  # the subset to send to, or None for every available one
    rule: str | None = None  # the rule that matched, if any


@dataclass(frozen=True, slots=True)
class _Rule:
    name: str
    camera: str | None
    event_types: frozenset[str]
    min_severity: Severity | None
    channels: frozenset[str] | None

    def matches(self, camera_id: str, event_type: str) -> bool:
        return (self.camera is None or self.camera == camera_id) and (
            not self.event_types or event_type in self.event_types
        )


@dataclass(frozen=True, slots=True)
class _Quiet:
    name: str
    schedule: Schedule
    let_through_from: Severity


def _schedule(config: QuietHoursConfig) -> Schedule:
    s = config.schedule
    return Schedule(
        start_minute=parse_clock(s.start),
        end_minute=parse_clock(s.end),
        utc_offset_minutes=s.utc_offset_minutes,
        weekdays=frozenset(s.weekdays),
    )


def _rule(config: AlertRuleConfig) -> _Rule:
    return _Rule(
        name=config.name,
        camera=config.camera,
        event_types=frozenset(config.event_types),
        min_severity=None if config.min_severity is None else Severity[config.min_severity],
        channels=None if config.channels is None else frozenset(config.channels),
    )


class AlertPolicy:
    """Immutable once built. A live change builds a new one and swaps the reference."""

    __slots__ = ("_default", "_enabled", "_quiet", "_rules")

    def __init__(self, config: AlertsConfig) -> None:
        self._enabled = config.enabled
        self._default = Severity[config.min_severity]
        self._rules = tuple(_rule(r) for r in config.rules)
        self._quiet = tuple(
            _Quiet(q.name, _schedule(q), Severity[q.let_through_from]) for q in config.quiet_hours
        )

    def decide(
        self,
        *,
        camera_id: str | None,
        event_type: str | None,
        severity: Severity,
        wall_utc: datetime,
    ) -> PolicyDecision:
        if not self._enabled:
            return PolicyDecision(False, "alerting is disabled in configuration", None)
        matched = next(
            (
                r
                for r in self._rules
                if camera_id is not None
                and event_type is not None
                and r.matches(camera_id, event_type)
            ),
            None,
        )
        threshold = self._default
        label = "alerts.min_severity"
        if matched is not None and matched.min_severity is not None:
            threshold = matched.min_severity
            label = f"rule {matched.name!r}"
        rule_name = None if matched is None else matched.name
        channels = None if matched is None else matched.channels
        if severity < threshold:
            return PolicyDecision(
                False,
                f"severity {severity.name} is below {label} ({threshold.name})",
                channels,
                rule_name,
            )
        for window in self._quiet:
            if severity < window.let_through_from and window.schedule.is_active_at(wall_utc):
                return PolicyDecision(
                    False,
                    f"held by quiet hours {window.name!r}: only {window.let_through_from.name} "
                    f"and above interrupt during this window (it is still recorded)",
                    channels,
                    rule_name,
                )
        return PolicyDecision(True, None, channels, rule_name)

    def held_by_quiet_hours(self, severity: Severity, wall_utc: datetime) -> str | None:
        """The name of a quiet window that would hold this severity now, for display."""
        for window in self._quiet:
            if severity < window.let_through_from and window.schedule.is_active_at(wall_utc):
                return window.name
        return None
