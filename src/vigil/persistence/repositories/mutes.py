"""Operator mutes (06 §10).

A mute is stored with a **wall-clock** expiry, because it has to survive a restart and a
monotonic clock does not. The validator that honours it works in monotonic time, so the
conversion happens at read time against the current clock — which is also why a mute's countdown
in the status bar stays correct across an NTP correction.

Mutes are revoked, never deleted: "who muted this camera, when, and why" is part of the audit
trail, and an expired mute that explains a quiet hour is worth keeping.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from vigil.domain.enums import EventType
from vigil.persistence.database import Database
from vigil.persistence.mappers import from_utc_text, to_utc_text

MUTE_COLUMNS = (
    "mute_id",
    "camera_id",
    "zone_id",
    "event_type",
    "created_utc",
    "expires_utc",
    "reason",
    "actor",
    "revoked_utc",
)


@dataclass(frozen=True, slots=True)
class MuteRecord:
    mute_id: str
    camera_id: str | None
    zone_id: str | None
    event_type: EventType | None
    created_wall_utc: datetime
    expires_wall_utc: datetime
    reason: str
    actor: str
    revoked_wall_utc: datetime | None = None

    def is_active_at(self, wall_utc: datetime) -> bool:
        return self.revoked_wall_utc is None and wall_utc < self.expires_wall_utc

    def remaining_seconds(self, wall_utc: datetime) -> float:
        return max(0.0, (self.expires_wall_utc - wall_utc).total_seconds())


def _record(row: object) -> MuteRecord:
    data: dict[str, object] = dict(row)  # type: ignore[call-overload]
    event_type = data["event_type"]
    revoked = data["revoked_utc"]
    return MuteRecord(
        mute_id=str(data["mute_id"]),
        camera_id=None if data["camera_id"] is None else str(data["camera_id"]),
        zone_id=None if data["zone_id"] is None else str(data["zone_id"]),
        event_type=None if event_type is None else EventType(str(event_type)),
        created_wall_utc=from_utc_text(str(data["created_utc"])),
        expires_wall_utc=from_utc_text(str(data["expires_utc"])),
        reason=str(data["reason"]),
        actor=str(data["actor"]),
        revoked_wall_utc=None if revoked is None else from_utc_text(str(revoked)),
    )


class MuteRepository:
    __slots__ = ("_db",)

    def __init__(self, database: Database) -> None:
        self._db = database

    def create(self, record: MuteRecord) -> None:
        with self._db.write(operation="insert_mute") as connection:
            connection.execute(
                f"INSERT INTO mutes ({', '.join(MUTE_COLUMNS)}) "
                "VALUES (:mute_id, :camera_id, :zone_id, :event_type, :created_utc, "
                ":expires_utc, :reason, :actor, :revoked_utc)",
                {
                    "mute_id": record.mute_id,
                    "camera_id": record.camera_id,
                    "zone_id": record.zone_id,
                    "event_type": None if record.event_type is None else record.event_type.value,
                    "created_utc": to_utc_text(record.created_wall_utc),
                    "expires_utc": to_utc_text(record.expires_wall_utc),
                    "reason": record.reason,
                    "actor": record.actor,
                    "revoked_utc": None,
                },
            )

    def revoke(self, mute_id: str, *, now_wall_utc: datetime) -> bool:
        with self._db.write(operation="revoke_mute") as connection:
            cursor = connection.execute(
                "UPDATE mutes SET revoked_utc = ? WHERE mute_id = ? AND revoked_utc IS NULL",
                (to_utc_text(now_wall_utc), mute_id),
            )
            revoked = cursor.rowcount == 1
            cursor.close()
        return revoked

    def active(self, now_wall_utc: datetime) -> tuple[MuteRecord, ...]:
        rows = self._db.query(
            f"SELECT {', '.join(MUTE_COLUMNS)} FROM mutes "
            "WHERE revoked_utc IS NULL AND expires_utc > ? ORDER BY expires_utc ASC",
            (to_utc_text(now_wall_utc),),
        )
        return tuple(_record(r) for r in rows)

    def all_recent(self, *, limit: int) -> tuple[MuteRecord, ...]:
        rows = self._db.query(
            f"SELECT {', '.join(MUTE_COLUMNS)} FROM mutes ORDER BY created_utc DESC LIMIT ?",
            (limit,),
        )
        return tuple(_record(r) for r in rows)

    def get(self, mute_id: str) -> MuteRecord | None:
        row = self._db.query_one(
            f"SELECT {', '.join(MUTE_COLUMNS)} FROM mutes WHERE mute_id = ?", (mute_id,)
        )
        return None if row is None else _record(row)

    def delete_expired_before(self, cutoff_wall_utc: datetime, *, limit: int) -> int:
        with self._db.write(operation="delete_mutes") as connection:
            cursor = connection.execute(
                "DELETE FROM mutes WHERE mute_id IN (SELECT mute_id FROM mutes "
                "WHERE expires_utc < ? ORDER BY expires_utc ASC LIMIT ?)",
                (to_utc_text(cutoff_wall_utc), limit),
            )
            removed = cursor.rowcount
            cursor.close()
        return max(removed, 0)


def mutes_for_camera(
    records: Sequence[MuteRecord], camera_id: str, *, now_wall_utc: datetime
) -> tuple[MuteRecord, ...]:
    """A mute with no camera applies to every camera; one with a camera applies only to it."""
    return tuple(
        r for r in records if r.is_active_at(now_wall_utc) and r.camera_id in (None, camera_id)
    )
