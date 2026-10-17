"""The webhook channel: the only outbound network this application makes (01 §10).

It exists because a local-first system still has to be able to tell something else. The
constraints that keep that honest:

  * **Operator-configured only.** The URL comes from `alerts.webhook_url` and from nowhere else.
    Nothing observed at runtime -- no detection, no incident field, no API input -- can choose
    where a request goes, which is what keeps this from becoming a request-forgery primitive.
  * **http or https only**, validated by the config model, which also refuses a URL carrying
    inline credentials.
  * **Bounded.** One timeout, a small attempt budget, and a fixed payload. No redirect chasing
    beyond urllib's default, no retry storm: an alert that cannot be delivered in three attempts
    is a delivery that has failed, and the failure is recorded on the alert.
  * **No evidence, no explanation, no pixels.** The payload is the incident's identity and
    severity. Anything more would be sending a scene over the network to tell someone a number.

`urllib.request` from the standard library rather than a client package: one POST of a small
JSON body does not justify a dependency (03 §1 rule 6).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from http import HTTPStatus

from vigil.core.protocols.logger import LoggerLike
from vigil.domain.alert import Alert, AlertDelivery
from vigil.response.channels.base import delivery_failed, delivery_ok

CHANNEL_ID = "webhook"
CONTENT_TYPE = "application/json"
USER_AGENT = "vigil-88"
_ALLOWED_SCHEMES = ("http://", "https://")
_MS_PER_S = 1000.0


class WebhookChannel:
    """Implements `core.protocols.channel.AlertChannel`."""

    __slots__ = ("_attempts", "_log", "_reason", "_timeout_s", "_url")

    def __init__(self, *, url: str, timeout_ms: int, max_attempts: int, logger: LoggerLike) -> None:
        self._url = url
        self._timeout_s = timeout_ms / _MS_PER_S
        self._attempts = max_attempts
        self._log = logger.bind(channel=CHANNEL_ID)
        self._reason: str | None = None

    @property
    def channel_id(self) -> str:
        return CHANNEL_ID

    def is_available(self) -> bool:
        """Configured and safe. Deliberately not a reachability probe: polling a receiver on
        every status read would be a side effect of looking at a dashboard."""
        if not self._url:
            self._reason = "alerts.webhook_url is not set"
            return False
        if not self._url.startswith(_ALLOWED_SCHEMES):
            self._reason = "alerts.webhook_url must start with http:// or https://"
            return False
        return True

    def unavailable_reason(self) -> str | None:
        """The reason recorded by the most recent `is_available()` or `send()`.

        Callers ask `is_available()` first (see `registry.AlertDispatcher.status`), so this
        reports a current reason rather than probing again on every read.
        """
        return self._reason

    def send(self, alert: Alert) -> AlertDelivery:
        """Never raises. Retries a transient failure up to the attempt budget."""
        if not self.is_available():
            return delivery_failed(CHANNEL_ID, self._reason or "not configured", attempts=0)
        payload = json.dumps(
            {
                "alert_id": alert.alert_id,
                "incident_id": alert.incident_id,
                "severity": alert.severity.name,
                "created_wall_utc": alert.created_wall_utc.isoformat(),
            },
            sort_keys=True,
        ).encode("utf-8")

        last_error = "no attempt was made"
        for attempt in range(1, self._attempts + 1):
            ok, last_error, retryable = self._post(payload)
            if ok:
                return delivery_ok(CHANNEL_ID, attempts=attempt)
            if not retryable:
                break
        return delivery_failed(CHANNEL_ID, last_error, attempts=self._attempts)

    def _post(self, payload: bytes) -> tuple[bool, str, bool]:
        """(delivered, error, worth retrying). A 4xx is the receiver saying "do not retry"."""
        request = urllib.request.Request(  # noqa: S310 - scheme validated above and in config
            self._url,
            data=payload,
            method="POST",
            headers={"Content-Type": CONTENT_TYPE, "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:  # noqa: S310
                status = int(response.status)
            if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
                return (True, "", False)
            return (False, f"the receiver returned HTTP {status}", True)
        except urllib.error.HTTPError as exc:
            retryable = int(exc.code) >= HTTPStatus.INTERNAL_SERVER_ERROR
            return (False, f"the receiver returned HTTP {exc.code}", retryable)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return (False, f"{type(exc).__name__}: {exc}", True)
