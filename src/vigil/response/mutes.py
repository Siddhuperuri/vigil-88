"""Operator mutes: durable, visible, and never silent (06 §10).

A mute is an operator saying *"I know, the contractors are working there until 5"*. It is a real
control with a real effect -- `OperatorSuppressionValidator` rejects candidates it covers -- so
three things have to be true at once:

  * **It survives a restart.** Stored with a wall-clock expiry, because a monotonic clock does
    not outlive the process. The validator works in monotonic time, so the conversion happens
    here, at read time, against the current clock; that also keeps a countdown correct across an
    NTP correction.
  * **It is visible.** `active()` is what the status strip renders with a countdown. A muted
    camera that looks normal is how a real incident gets missed, so there is no code path that
    applies a mute without also being able to show it.
  * **It is cheap to consult.** The validator asks once per analysed frame per camera, so a
    database round trip per frame would put storage latency on the analysis thread. The active
    set is cached for `_CACHE_TTL_MS` and invalidated whenever a mute is created or revoked.

Mutes are revoked rather than deleted: "who muted this, when, and why" is part of the audit
trail, and an expired mute that explains a quiet hour is worth keeping.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from vigil.core.clock import Clock
from vigil.core.ids import UlidFactory
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import NS_PER_S
from vigil.domain.enums import EventType
from vigil.domain.mute import Mute
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.repositories.mutes import MuteRecord, MuteRepository

# Long enough that the analysis thread is not querying storage every frame, short enough that a
# mute an operator just created takes effect on the next second of footage.
_CACHE_TTL_MS = 1000.0
MAX_MUTE_SECONDS = 24 * 60 * 60


class MuteRegistry:
    """One per process. Read from analysis threads, written from the API thread, so it locks."""

    __slots__ = ("_cache", "_clock", "_fetched_ns", "_lock", "_log", "_metrics", "_repo", "_ulids")

    def __init__(
        self,
        *,
        repository: MuteRepository,
        clock: Clock,
        ulids: UlidFactory,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._repo = repository
        self._clock = clock
        self._ulids = ulids
        self._log = logger.bind(component="mutes")
        self._metrics = metrics
        self._lock = threading.Lock()
        self._cache: tuple[MuteRecord, ...] = ()
        self._fetched_ns: int | None = None

    # ---------------------------------------------------------------- writes

    def create(
        self,
        *,
        duration_s: float,
        reason: str,
        actor: str = "operator",
        camera_id: str | None = None,
        zone_id: str | None = None,
        event_type: EventType | None = None,
    ) -> MuteRecord:
        """Create a mute. Bounded duration: an unbounded mute is a disabled camera in disguise."""
        if duration_s <= 0.0:
            raise ValueError("a mute needs a positive duration")
        capped = min(duration_s, float(MAX_MUTE_SECONDS))
        now = self._clock.wall_utc()
        record = MuteRecord(
            mute_id=self._ulids.new(),
            camera_id=camera_id,
            zone_id=zone_id,
            event_type=event_type,
            created_wall_utc=now,
            expires_wall_utc=now + timedelta(seconds=capped),
            reason=reason or "operator mute",
            actor=actor,
        )
        self._repo.create(record)
        self._invalidate()
        self._metrics.counter("vigil_mutes_created_total").inc()
        self._log.warning(
            "mute_created",
            mute_id=record.mute_id,
            camera_id=camera_id or "(all)",
            zone_id=zone_id,
            event_type=None if event_type is None else event_type.value,
            expires_wall_utc=record.expires_wall_utc.isoformat(),
            duration_s=round(capped, 1),
            reason=record.reason,
            actor=actor,
        )
        return record

    def revoke(self, mute_id: str) -> bool:
        revoked = self._repo.revoke(mute_id, now_wall_utc=self._clock.wall_utc())
        if revoked:
            self._invalidate()
            self._metrics.counter("vigil_mutes_revoked_total").inc()
            self._log.info("mute_revoked", mute_id=mute_id)
        return revoked

    def _invalidate(self) -> None:
        with self._lock:
            self._fetched_ns = None

    # ---------------------------------------------------------------- reads

    def active(self) -> tuple[MuteRecord, ...]:
        """Every mute in force now, soonest to expire first. What the status strip renders."""
        now_wall = self._clock.wall_utc()
        now_ns = self._clock.monotonic_ns()
        with self._lock:
            fresh = (
                self._fetched_ns is not None
                and (now_ns - self._fetched_ns) / 1_000_000 < _CACHE_TTL_MS
            )
            if fresh:
                return tuple(r for r in self._cache if r.is_active_at(now_wall))
        records = self._repo.active(now_wall)
        with self._lock:
            self._cache = records
            self._fetched_ns = now_ns
        self._metrics.gauge("vigil_mutes_active").set(float(len(records)))
        return records

    def mutes_for(self, camera_id: str) -> tuple[Mute, ...]:
        """The validator's form: monotonic expiry, scoped to one camera.

        A mute with no camera applies to every camera; one with a camera applies only to it.
        """
        now_wall = self._clock.wall_utc()
        now_ns = self._clock.monotonic_ns()
        out: list[Mute] = []
        for record in self.active():
            if record.camera_id not in (None, camera_id):
                continue
            remaining_s = record.remaining_seconds(now_wall)
            out.append(
                Mute(
                    camera_id=camera_id,
                    event_type=record.event_type,
                    zone_id=record.zone_id,
                    expires_monotonic_ns=now_ns + int(remaining_s * NS_PER_S),
                    reason=record.reason,
                )
            )
        return tuple(out)

    def recent(self, *, limit: int = 50) -> tuple[MuteRecord, ...]:
        """Including expired and revoked ones: the audit view, not the live one."""
        return self._repo.all_recent(limit=limit)

    def get(self, mute_id: str) -> MuteRecord | None:
        return self._repo.get(mute_id)
