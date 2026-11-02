"""The audit log: append-only, hash-chained, in the account database (plan §7.8).

Each row stores a SHA-256 over the previous row's hash and its own content, so editing or deleting a
row in the middle breaks every hash after it and `verify()` reports where. That makes tampering
**evident**. It does not make it impossible: someone holding the file can recompute the whole chain,
and the threat model says so rather than implying otherwise.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from vigil.auth.models import AuditEntry, Principal
from vigil.auth.store import AccountStore
from vigil.core.clock import Clock

GENESIS = "0" * 64
MAX_DETAIL_BYTES = 4000


def _row_hash(
    prev: str,
    at: str,
    user_id: str | None,
    username: str | None,
    action: str,
    target: str | None,
    detail: str,
) -> str:
    body = json.dumps(
        [prev, at, user_id, username, action, target, detail],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(body.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainReport:
    ok: bool
    rows: int
    first_bad_seq: int | None  # the first row whose hash or link does not hold


class AuditLog:
    def __init__(self, store: AccountStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def record(
        self,
        action: str,
        *,
        actor: Principal | None = None,
        user_id: str | None = None,
        username: str | None = None,
        target: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> AuditEntry:
        """Append one row. The actor is a parameter, never looked up, so a failed login (which has
        no principal) and a system action (which has none either) both record honestly."""
        if actor is not None:
            user_id, username = actor.user_id, actor.username
        text = json.dumps(dict(detail or {}), sort_keys=True, default=str, ensure_ascii=True)
        if len(text) > MAX_DETAIL_BYTES:
            text = json.dumps({"truncated": True, "bytes": len(text)})
        at = self._clock.wall_utc()
        with self._store.transaction() as db:
            tip = db.execute("SELECT row_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = GENESIS if tip is None else str(tip[0])
            digest = _row_hash(prev, at.isoformat(), user_id, username, action, target, text)
            cur = db.execute(
                "INSERT INTO audit (at_utc, user_id, username, action, target, detail, prev_hash,"
                " row_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (at.isoformat(), user_id, username, action, target, text, prev, digest),
            )
            seq = int(cur.lastrowid or 0)
        return AuditEntry(seq, at, user_id, username, action, target, text, prev, digest)

    def entries(
        self, *, limit: int = 100, before_seq: int | None = None, action: str | None = None
    ) -> list[AuditEntry]:
        """Newest first. Keyset-paged on `seq`, like the incident list. Four fixed statements
        rather than one assembled from fragments: nothing here is ever built from input."""
        cap = max(1, min(limit, 1000))
        if before_seq is None and action is None:
            rows = self._store.query("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (cap,))
        elif action is None:
            rows = self._store.query(
                "SELECT * FROM audit WHERE seq < ? ORDER BY seq DESC LIMIT ?", (before_seq, cap)
            )
        elif before_seq is None:
            rows = self._store.query(
                "SELECT * FROM audit WHERE action = ? ORDER BY seq DESC LIMIT ?", (action, cap)
            )
        else:
            rows = self._store.query(
                "SELECT * FROM audit WHERE seq < ? AND action = ? ORDER BY seq DESC LIMIT ?",
                (before_seq, action, cap),
            )
        return [self._entry(r) for r in rows]

    def verify(self) -> ChainReport:
        prev = GENESIS
        count = 0
        for r in self._store.query("SELECT * FROM audit ORDER BY seq ASC"):
            count += 1
            expected = _row_hash(
                prev,
                r["at_utc"],
                r["user_id"],
                r["username"],
                r["action"],
                r["target"],
                r["detail"],
            )
            if r["prev_hash"] != prev or r["row_hash"] != expected:
                return ChainReport(ok=False, rows=count, first_bad_seq=int(r["seq"]))
            prev = r["row_hash"]
        return ChainReport(ok=True, rows=count, first_bad_seq=None)

    @staticmethod
    def _entry(row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            seq=int(row["seq"]),
            at_utc=datetime.fromisoformat(row["at_utc"]),
            user_id=row["user_id"],
            username=row["username"],
            action=row["action"],
            target=row["target"],
            detail=row["detail"],
            prev_hash=row["prev_hash"],
            row_hash=row["row_hash"],
        )
