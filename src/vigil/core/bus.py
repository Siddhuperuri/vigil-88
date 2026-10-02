"""In-process typed pub/sub.

Dispatch is synchronous, on the publisher's thread, and happens outside the lock. One
failing subscriber never affects the publisher or the other subscribers; the failure is
reported through `on_handler_error` (wired to a metric by the application) and, by default,
logged at WARNING. It is never silently swallowed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TypeVar, cast

E = TypeVar("E")
Handler = Callable[[object], None]
ErrorHook = Callable[[object, BaseException], None]

_log = logging.getLogger(__name__)


class Subscription:
    def __init__(self, bus: EventBus, token: int) -> None:
        self._bus = bus
        self._token = token

    def cancel(self) -> None:
        self._bus._unsubscribe(self._token)


class EventBus:
    def __init__(self, on_handler_error: ErrorHook | None = None) -> None:
        self._lock = threading.Lock()
        self._next_token = 0
        self._subs: dict[int, tuple[type, Handler]] = {}
        self._on_handler_error = on_handler_error or self._default_error_hook

    @staticmethod
    def _default_error_hook(event: object, exc: BaseException) -> None:
        _log.warning(
            "event bus subscriber failed for %s: %r", type(event).__name__, exc, exc_info=exc
        )

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> Subscription:
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._subs[token] = (event_type, cast(Handler, handler))
        return Subscription(self, token)

    def _unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subs.pop(token, None)

    def publish(self, event: object) -> int:
        """Deliver to every matching subscriber. Returns the number of handlers invoked."""
        with self._lock:
            targets = [h for t, h in self._subs.values() if isinstance(event, t)]
        for handler in targets:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - subscriber isolation boundary
                self._on_handler_error(event, exc)
        return len(targets)
