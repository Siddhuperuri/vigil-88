"""The JSONL channel: one JSON object per alert, appended to a file (04 §3.6).

Useful because it is the simplest thing another system can tail, and because an append-only
file is the easiest alert record to reason about after an incident review.

Availability is tested by actually opening the file for append, not by guessing from the path:
a directory that exists is no proof it can be written to, and the whole point of
`is_available()` is that it must not lie.
"""

from __future__ import annotations

import json
from pathlib import Path

from vigil.core.protocols.logger import LoggerLike
from vigil.domain.alert import Alert, AlertDelivery
from vigil.response.channels.base import delivery_failed, delivery_ok

CHANNEL_ID = "jsonl"


class JsonlChannel:
    """Implements `core.protocols.channel.AlertChannel`."""

    __slots__ = ("_log", "_path", "_reason")

    def __init__(self, path: Path, *, logger: LoggerLike) -> None:
        self._path = path
        self._log = logger.bind(channel=CHANNEL_ID)
        self._reason: str | None = None

    @property
    def channel_id(self) -> str:
        return CHANNEL_ID

    @property
    def path(self) -> Path:
        return self._path

    def is_available(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8"):
                pass
        except OSError as exc:
            self._reason = f"cannot append to {self._path.name}: {exc}"
            return False
        self._reason = None
        return True

    def unavailable_reason(self) -> str | None:
        return self._reason

    def send(self, alert: Alert) -> AlertDelivery:
        """Never raises. One line per alert, flushed, so a tail sees it immediately."""
        record = {
            "alert_id": alert.alert_id,
            "incident_id": alert.incident_id,
            "severity": alert.severity.name,
            "created_wall_utc": alert.created_wall_utc.isoformat(),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._reason = reason
            return delivery_failed(CHANNEL_ID, reason)
        return delivery_ok(CHANNEL_ID)
