"""Operator mutes (06 §10).

A value object, so it lives here rather than in either engine that touches it. `response/`
creates mutes and `verification/` honours them, and both are L4 siblings that must not import
each other (01 §5) -- the shared type belongs in the layer below them both, which is also where
02 §3 sends anything that is a frozen value with no behaviour beyond validation.

Expiry is **monotonic** because that is what a validator compares against a frame's timestamp.
The durable form in the database carries a wall-clock expiry, and `response/mutes.py` converts
between them at read time; a monotonic instant cannot be stored, and a wall clock cannot safely
be compared against frame times (05 §1 rule 4).
"""

from __future__ import annotations

from dataclasses import dataclass

from vigil.domain.enums import EventType


@dataclass(frozen=True, slots=True)
class Mute:
    """An operator saying "I know, the contractors are working there until 5".

    A mute is never silent: it is visible on the status bar with a countdown, because a muted
    camera that looks normal is how a real incident gets missed.

    Scope narrows as fields are set: a mute with no `event_type` covers every type on that
    camera, and one with no `zone_id` covers the whole view.
    """

    camera_id: str
    event_type: EventType | None = None
    zone_id: str | None = None
    expires_monotonic_ns: int | None = None
    reason: str = "operator mute"

    def covers(
        self,
        *,
        camera_id: str,
        event_type: EventType,
        zone_ids: frozenset[str],
        now_ns: int,
    ) -> bool:
        if self.camera_id != camera_id:
            return False
        if self.expires_monotonic_ns is not None and now_ns >= self.expires_monotonic_ns:
            return False
        if self.event_type is not None and self.event_type is not event_type:
            return False
        return not (self.zone_id is not None and self.zone_id not in zone_ids)
