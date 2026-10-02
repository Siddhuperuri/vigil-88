"""Alert channel interface (04 §3.6). No channel is implemented in P0/P1.

`is_available()` is why channels cannot be fake: an unavailable channel is reported as
unavailable and does not accept sends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.domain.alert import Alert, AlertDelivery


class AlertChannel(Protocol):
    @property
    def channel_id(self) -> str: ...

    def is_available(self) -> bool: ...

    def send(self, alert: Alert) -> AlertDelivery:
        """Never raises: failure is reported in the returned delivery."""
        ...
