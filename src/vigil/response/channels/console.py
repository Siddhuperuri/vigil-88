"""The console channel: a structured log line per alert (04 §3.6).

Always available, because it has no dependency that can be missing. It is the channel that
makes "no alert channel configured" impossible to reach silently: whatever else is switched off,
a dispatched alert leaves a record somewhere an operator can find.

It logs the incident's identity and severity, never its evidence paths or its explanation.
Logging is not the explanation mechanism (01 §7.2): the explanation is structured data attached
to the incident, because log files are not queryable from the console.
"""

from __future__ import annotations

from vigil.core.protocols.logger import LoggerLike
from vigil.domain.alert import Alert, AlertDelivery
from vigil.response.channels.base import delivery_failed, delivery_ok

CHANNEL_ID = "console"


class ConsoleChannel:
    """Implements `core.protocols.channel.AlertChannel`."""

    __slots__ = ("_log",)

    def __init__(self, logger: LoggerLike) -> None:
        self._log = logger.bind(channel=CHANNEL_ID)

    @property
    def channel_id(self) -> str:
        return CHANNEL_ID

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str | None:
        return None

    def send(self, alert: Alert) -> AlertDelivery:
        """Never raises: a failure is reported in the returned delivery."""
        try:
            self._log.warning(
                "alert",
                alert_id=alert.alert_id,
                incident_id=alert.incident_id,
                severity=alert.severity.name,
                created_wall_utc=alert.created_wall_utc.isoformat(),
            )
        except OSError as exc:  # a closed or full log sink
            return delivery_failed(CHANNEL_ID, f"{type(exc).__name__}: {exc}")
        return delivery_ok(CHANNEL_ID)
