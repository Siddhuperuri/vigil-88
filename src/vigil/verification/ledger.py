"""Deduplication, cooldown and rate limiting state (06 §6 validators 7-9).

This is the memory the verification engine needs across candidates, and the reason it is a
separate object: a validator sees one candidate, and these three questions are about the
relationship between a candidate and everything that came before it.

The dedup key is `(camera_id, event_type, subject)`. That tuple is the identity of a *situation*
— one person in one zone on one camera — so a second candidate for the same key is the same
situation continuing, and must extend the open event rather than start another.

No clock is read. Time arrives from the candidate's own timestamp.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

from vigil.config.schema.modules import VerificationConfig
from vigil.core.units import ns_to_ms
from vigil.domain.enums import EventType
from vigil.domain.observation import SubjectKey

DedupKey = tuple[str, EventType, SubjectKey]


def dedup_key(camera_id: str, event_type: EventType, subject: SubjectKey) -> DedupKey:
    """The identity of a situation, defined in exactly one place."""
    return (camera_id, event_type, subject)


@dataclass(slots=True)
class OpenEvent:
    """A situation currently being reported. P4 turns this into an incident; P3 only needs to
    know that it exists, so that a second candidate extends rather than duplicates it."""

    key: DedupKey
    candidate_id: str
    opened_monotonic_ns: int
    last_observed_monotonic_ns: int
    extensions: int = 0

    def extend(self, t_monotonic_ns: int) -> None:
        self.last_observed_monotonic_ns = max(self.last_observed_monotonic_ns, t_monotonic_ns)
        self.extensions += 1

    def duration_ms(self, now_ns: int) -> float:
        return ns_to_ms(max(now_ns, self.last_observed_monotonic_ns) - self.opened_monotonic_ns)


class SuppressionLedger:
    """Open events, recent closures and a per-camera rate window. One instance per pipeline."""

    __slots__ = ("_closed_ns", "_config", "_open", "_recent_by_camera")

    def __init__(self, config: VerificationConfig) -> None:
        self._config = config
        self._open: dict[DedupKey, OpenEvent] = {}
        self._closed_ns: dict[DedupKey, int] = {}
        self._recent_by_camera: dict[str, deque[int]] = {}

    # ---------------------------------------------------------------- open events (validator 7)

    def open_event(self, key: DedupKey) -> OpenEvent | None:
        return self._open.get(key)

    def is_open(self, key: DedupKey) -> bool:
        return key in self._open

    def open_keys(self) -> tuple[DedupKey, ...]:
        return tuple(sorted(self._open, key=lambda k: (k[0], k[1].value, k[2].kind, k[2].value)))

    def __len__(self) -> int:
        return len(self._open)

    def __iter__(self) -> Iterator[OpenEvent]:
        return iter(self._open[k] for k in self.open_keys())

    def record_open(self, key: DedupKey, *, candidate_id: str, t_monotonic_ns: int) -> OpenEvent:
        existing = self._open.get(key)
        if existing is not None:
            existing.extend(t_monotonic_ns)
            return existing
        event = OpenEvent(
            key=key,
            candidate_id=candidate_id,
            opened_monotonic_ns=t_monotonic_ns,
            last_observed_monotonic_ns=t_monotonic_ns,
        )
        self._open[key] = event
        self._recent_by_camera.setdefault(key[0], deque()).append(t_monotonic_ns)
        return event

    def extend_open(self, key: DedupKey, t_monotonic_ns: int) -> OpenEvent | None:
        event = self._open.get(key)
        if event is not None:
            event.extend(t_monotonic_ns)
        return event

    def close(self, key: DedupKey, t_monotonic_ns: int) -> OpenEvent | None:
        """End a situation and start its cooldown."""
        event = self._open.pop(key, None)
        self._closed_ns[key] = t_monotonic_ns
        return event

    # ---------------------------------------------------------------- cooldown (validator 8)

    def closed_at(self, key: DedupKey) -> int | None:
        return self._closed_ns.get(key)

    def in_cooldown(self, key: DedupKey, now_ns: int) -> bool:
        """True while a re-fire would be oscillation rather than a new situation."""
        closed = self._closed_ns.get(key)
        if closed is None:
            return False
        return ns_to_ms(now_ns - closed) < self._config.cooldown_ms

    def cooldown_remaining_ms(self, key: DedupKey, now_ns: int) -> float:
        closed = self._closed_ns.get(key)
        if closed is None:
            return 0.0
        return max(0.0, self._config.cooldown_ms - ns_to_ms(now_ns - closed))

    # ---------------------------------------------------------------- rate limit (validator 9)

    def recent_count(self, camera_id: str, now_ns: int) -> int:
        """Events opened on this camera inside the rate window."""
        window = self._recent_by_camera.get(camera_id)
        if window is None:
            return 0
        self._trim(window, now_ns)
        return len(window)

    def _trim(self, window: deque[int], now_ns: int) -> None:
        horizon_ms = self._config.rate_limit_window_ms
        while window and ns_to_ms(now_ns - window[0]) >= horizon_ms:
            window.popleft()

    def prune(self, now_ns: int) -> None:
        """Drop cooldown entries and rate samples that can no longer affect a decision.

        Called on the analysis tick. Without it the ledger grows one entry per subject the
        system has ever seen, which on a busy camera is a slow leak.
        """
        expired = [
            key
            for key, closed in self._closed_ns.items()
            if ns_to_ms(now_ns - closed) >= self._config.cooldown_ms
        ]
        for key in expired:
            del self._closed_ns[key]
        for camera_id in tuple(self._recent_by_camera):
            window = self._recent_by_camera[camera_id]
            self._trim(window, now_ns)
            if not window:
                del self._recent_by_camera[camera_id]

    def reset_camera(self, camera_id: str) -> None:
        """Forget a camera's state. Used on a tracker epoch change: subject identities no longer
        mean what they meant, so dedup keys built from them are meaningless."""
        for key in [k for k in self._open if k[0] == camera_id]:
            del self._open[key]
        for key in [k for k in self._closed_ns if k[0] == camera_id]:
            del self._closed_ns[key]
        self._recent_by_camera.pop(camera_id, None)
