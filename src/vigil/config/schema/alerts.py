"""Alert channel settings (04 §3.6, 06 §10).

An unconfigured channel is **absent**, not stubbed: `channels` is an explicit allowlist, and a
channel that cannot work reports `is_available()` false and is not offered in the UI.

Severity is named here as a string Literal rather than imported from `domain.enums.Severity`,
because `config/` is L1 and `domain/` is L2 and the layering forbids the import (01 §5). See
`schema/taxonomy.py` for the full reasoning; a test checks the mirror against the enum, so the
two cannot drift.

**Rules and quiet hours only ever narrow what is sent, and never touch what is recorded.** An
incident is stored whatever its alert policy says; a rule or a quiet window decides whether
somebody is *interrupted*. CRITICAL cannot be held back by a quiet window: the setting that would
do it does not exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from vigil.config.schema.base import ConfigModel
from vigil.config.schema.taxonomy import EVENT_TYPE_NAMES
from vigil.config.schema.zones import ZONE_ID_PATTERN, ScheduleConfig
from vigil.core.redaction import contains_inline_credentials

ChannelName = Literal["console", "jsonl", "webhook"]
# Mirrors domain.enums.Severity; `schema/taxonomy.py` explains why, and a test keeps it honest.
SeverityName = Literal["INFO", "LOW", "MODERATE", "HIGH", "CRITICAL"]
_ALLOWED_SCHEMES = ("http://", "https://")
MAX_RULES = 32
MAX_QUIET_WINDOWS = 8


class AlertRuleConfig(ConfigModel):
    """When an incident matches, this rule's threshold and channels apply instead of the default.

    The first matching rule wins. A rule narrows by camera and event type (an empty list or no
    camera means any); it may raise or lower the severity threshold for what it matches, and may
    send to a subset of the configured channels.
    """

    name: str = Field(min_length=1, max_length=64)
    camera: str | None = None
    event_types: tuple[str, ...] = ()
    min_severity: SeverityName | None = None
    channels: tuple[ChannelName, ...] | None = None

    @field_validator("camera")
    @classmethod
    def _camera_slug(cls, value: str | None) -> str | None:
        if value is not None and not ZONE_ID_PATTERN.match(value):
            raise ValueError(f"camera {value!r} is not a camera id")
        return value

    @field_validator("event_types")
    @classmethod
    def _known_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - EVENT_TYPE_NAMES)
        if unknown:
            raise ValueError(f"unknown event types {unknown}; known: {sorted(EVENT_TYPE_NAMES)}")
        return value


class QuietHoursConfig(ConfigModel):
    """A window during which only alerts at or above `let_through_from` interrupt anyone.

    `let_through_from` defaults to CRITICAL and cannot be set higher, because there is nothing
    higher: CRITICAL always gets through.
    """

    name: str = Field(min_length=1, max_length=64)
    schedule: ScheduleConfig
    let_through_from: SeverityName = "CRITICAL"


class AlertsConfig(ConfigModel):
    enabled: bool = True
    # Explicit allowlist. Discovering a channel never enables it, exactly as for modules.
    channels: tuple[ChannelName, ...] = ("console",)
    # Alerts below this band are recorded as incidents but not dispatched: an alert storm
    # destroys the operator's ability to see anything, which is worse than a missed alert.
    min_severity: SeverityName = "MODERATE"
    # Per-camera and per-type overrides, first match wins (see AlertRuleConfig).
    rules: tuple[AlertRuleConfig, ...] = Field(default=(), max_length=MAX_RULES)
    # Windows in which lower-severity alerts are held. They are still recorded as incidents.
    quiet_hours: tuple[QuietHoursConfig, ...] = Field(default=(), max_length=MAX_QUIET_WINDOWS)
    # Default: <logs_dir>/alerts.jsonl
    jsonl_file: Path | None = None
    # The only outbound network this application makes, and only when set (01 §10).
    webhook_url: str | None = None
    webhook_timeout_ms: int = Field(default=3000, ge=100, le=30_000)
    webhook_max_attempts: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def _webhook_is_safe(self) -> Self:
        url = self.webhook_url
        if url is None:
            if "webhook" in self.channels:
                raise ValueError(
                    "the webhook channel is enabled but alerts.webhook_url is not set; "
                    "a channel that cannot deliver must not be listed"
                )
            return self
        if not url.startswith(_ALLOWED_SCHEMES):
            raise ValueError("alerts.webhook_url must start with http:// or https://")
        if contains_inline_credentials(url):
            raise ValueError(
                "alerts.webhook_url must not contain credentials; put the secret in a header "
                "value referenced as ${env:NAME} or use a signed URL from the receiving system"
            )
        return self

    @model_validator(mode="after")
    def _rules_are_coherent(self) -> Self:
        names = [r.name for r in self.rules]
        if len(names) != len(set(names)):
            raise ValueError("alert rule names must be unique")
        quiet = [w.name for w in self.quiet_hours]
        if len(quiet) != len(set(quiet)):
            raise ValueError("quiet-hours names must be unique")
        for rule in self.rules:
            if rule.channels is None:
                continue
            missing = sorted(set(rule.channels) - set(self.channels))
            if missing:
                raise ValueError(
                    f"rule {rule.name!r} sends to {missing}, which are not in alerts.channels"
                )
        return self
