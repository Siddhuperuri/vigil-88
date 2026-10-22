"""Building the configured channel set, and dispatching to it (04 §3.6, 06 §10).

The anti-fake-functionality rule applied to outputs. Three things follow from it:

  * a channel the operator did not list is **not built**;
  * a channel that is listed but cannot work (no URL, an unwritable path) reports
    `is_available()` false with a reason, is excluded from dispatch, and is shown that way in
    the API rather than appearing to work;
  * a send never raises. Failure comes back as an `AlertDelivery` with its attempt count and
    error, which is persisted on the alert, because "we tried three times and the receiver
    refused" is operational information an operator needs.

Severity gating happens here rather than in each channel, so "below `min_severity`" means the
same thing everywhere and is visible in one place.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from vigil.config.schema.alerts import AlertsConfig
from vigil.core.clock import Clock
from vigil.core.ids import UlidFactory
from vigil.core.protocols.logger import LoggerLike
from vigil.domain.alert import Alert, AlertDelivery
from vigil.domain.enums import Severity
from vigil.observability.metrics import MetricsRegistry
from vigil.response.channels.base import Channel, ChannelStatus
from vigil.response.channels.console import ConsoleChannel
from vigil.response.channels.jsonl import JsonlChannel
from vigil.response.channels.webhook import WebhookChannel


@dataclass(frozen=True, slots=True)
class DispatchResult:
    alert: Alert | None
    suppressed_reason: str | None = None

    @property
    def dispatched(self) -> bool:
        return self.alert is not None


def build_channels(
    config: AlertsConfig,
    *,
    logger: LoggerLike,
    alerts_file: Path,
) -> tuple[Channel, ...]:
    """Construct exactly the channels the operator listed, in a deterministic order."""
    built: list[Channel] = []
    for name in config.channels:
        if name == "console":
            built.append(ConsoleChannel(logger))
        elif name == "jsonl":
            built.append(JsonlChannel(alerts_file, logger=logger))
        elif name == "webhook":
            built.append(
                WebhookChannel(
                    url=config.webhook_url or "",
                    timeout_ms=config.webhook_timeout_ms,
                    max_attempts=config.webhook_max_attempts,
                    logger=logger,
                )
            )
    return tuple(built)


class AlertDispatcher:
    """Sends one alert to every available channel. Holds no state beyond the channel set."""

    __slots__ = ("_channels", "_clock", "_config", "_log", "_metrics", "_threshold", "_ulids")

    def __init__(
        self,
        *,
        channels: Sequence[Channel],
        config: AlertsConfig,
        clock: Clock,
        ulids: UlidFactory,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._channels = tuple(channels)
        self._config = config
        self._clock = clock
        self._ulids = ulids
        self._log = logger.bind(component="alerts")
        self._metrics = metrics
        self._threshold = Severity[config.min_severity]

    @property
    def channel_ids(self) -> tuple[str, ...]:
        return tuple(c.channel_id for c in self._channels)

    def status(self) -> tuple[ChannelStatus, ...]:
        """Every channel the operator listed, with whether it can actually deliver."""
        configured = set(self._config.channels)
        statuses = [
            ChannelStatus(
                channel_id=c.channel_id,
                configured=True,
                available=c.is_available(),
                reason=c.unavailable_reason(),
            )
            for c in self._channels
        ]
        # A channel named in config that failed to build at all is still reported, so the UI can
        # say why rather than quietly omitting it.
        built = {c.channel_id for c in self._channels}
        statuses.extend(
            ChannelStatus(channel_id=name, configured=True, available=False, reason="not built")
            for name in sorted(configured - built)
        )
        return tuple(statuses)

    def dispatch(self, incident_id: str, severity: Severity) -> DispatchResult:
        """Send an alert for one incident, or say why it was not sent."""
        if not self._config.enabled:
            return DispatchResult(None, "alerting is disabled in configuration")
        if severity < self._threshold:
            return DispatchResult(
                None,
                f"severity {severity.name} is below alerts.min_severity ({self._threshold.name})",
            )
        available = [c for c in self._channels if c.is_available()]
        if not available:
            return DispatchResult(None, "no alert channel is available")

        deliveries: list[AlertDelivery] = []
        for channel in available:
            delivery = channel.send(
                Alert(
                    alert_id=self._ulids.new(),
                    incident_id=incident_id,
                    severity=severity,
                    created_wall_utc=self._clock.wall_utc(),
                    channels=(channel.channel_id,),
                    deliveries=(),
                )
            )
            deliveries.append(delivery)
            self._metrics.counter(
                "vigil_alerts_sent_total",
                channel=channel.channel_id,
                outcome="ok" if delivery.ok else "failed",
            ).inc()
            if not delivery.ok:
                self._log.warning(
                    "alert_delivery_failed",
                    incident_id=incident_id,
                    channel=channel.channel_id,
                    attempts=delivery.attempts,
                    error=delivery.error,
                )
        alert = Alert(
            alert_id=self._ulids.new(),
            incident_id=incident_id,
            severity=severity,
            created_wall_utc=self._clock.wall_utc(),
            channels=tuple(c.channel_id for c in available),
            deliveries=tuple(deliveries),
        )
        return DispatchResult(alert)
