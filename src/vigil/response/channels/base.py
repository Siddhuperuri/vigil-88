"""The alert-channel contract and its verdict helpers.

Separated from `registry.py` so the dependency runs one way only: every channel imports this,
and the registry imports both this and the channels. With the helpers living in the registry,
`registry -> console -> registry` was a genuine import cycle, which
`tests/unit/test_gates_and_architecture.py` catches and which would eventually bite as a partly
initialised module at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from vigil.domain.alert import Alert, AlertDelivery


def delivery_ok(channel_id: str, *, attempts: int = 1) -> AlertDelivery:
    return AlertDelivery(channel_id=channel_id, ok=True, attempts=attempts, error=None)


def delivery_failed(channel_id: str, error: str, *, attempts: int = 1) -> AlertDelivery:
    return AlertDelivery(channel_id=channel_id, ok=False, attempts=attempts, error=error)


class Channel(Protocol):
    """`core.protocols.channel.AlertChannel`, plus the reason an unavailable one is unavailable.

    `send` never raises: failure comes back as a delivery with its attempt count and error, so
    "we tried three times and the receiver refused" survives as operational information.
    """

    @property
    def channel_id(self) -> str: ...

    def is_available(self) -> bool: ...

    def unavailable_reason(self) -> str | None: ...

    def send(self, alert: Alert) -> AlertDelivery: ...


@dataclass(frozen=True, slots=True)
class ChannelStatus:
    """What the API serves for "which alert channels work, and why not" (08 §8)."""

    channel_id: str
    configured: bool
    available: bool
    reason: str | None
