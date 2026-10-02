"""Alerts and their per-channel deliveries (05 §4). Alerting arrives after P4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vigil.core.ids import is_valid_ulid
from vigil.domain._validate import require_non_empty, require_non_negative, require_utc
from vigil.domain.enums import Severity


@dataclass(frozen=True, slots=True)
class AlertDelivery:
    channel_id: str
    ok: bool
    attempts: int
    error: str | None

    def __post_init__(self) -> None:
        require_non_empty("channel_id", self.channel_id)
        require_non_negative("attempts", self.attempts)
        if self.ok and self.error is not None:
            raise ValueError("a successful delivery cannot carry an error")


@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: str
    incident_id: str
    severity: Severity
    created_wall_utc: datetime
    channels: tuple[str, ...]
    deliveries: tuple[AlertDelivery, ...]

    def __post_init__(self) -> None:
        if not is_valid_ulid(self.alert_id) or not is_valid_ulid(self.incident_id):
            raise ValueError("alert_id and incident_id must be ULIDs")
        require_utc("created_wall_utc", self.created_wall_utc)
        object.__setattr__(self, "channels", tuple(self.channels))
        object.__setattr__(self, "deliveries", tuple(self.deliveries))
