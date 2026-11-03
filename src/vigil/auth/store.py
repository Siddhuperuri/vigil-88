"""The account database: its own SQLite file, apart from incidents (ADR 0016).

Incident retention, a vacuum or a restore of the incident database cannot touch accounts or the
audit chain, and deleting evidence cannot lose them. One connection behind a lock: the API calls
in from a thread pool and the volume is a login now and then.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from vigil.auth.models import Lockout, SessionRecord, User
from vigil.auth.roles import Role
from vigil.core.errors import PersistenceError

SCHEMA_VERSION = 1

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        role TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0,
        created_utc TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_name ON users (username COLLATE NOCASE)",
    """CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        created_utc TEXT NOT NULL,
        last_seen_utc TEXT NOT NULL,
        expires_utc TEXT NOT NULL,
        locked INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id)",
    """CREATE TABLE IF NOT EXISTS recovery_codes (
        code_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        code_hash TEXT NOT NULL,
        used_utc TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS lockouts (
        key TEXT PRIMARY KEY,
        failures INTEGER NOT NULL,
        locked_until_utc TEXT,
        last_failure_utc TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at_utc TEXT NOT NULL,
        user_id TEXT,
        username TEXT,
        action TEXT NOT NULL,
        target TEXT,
        detail TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        row_hash TEXT NOT NULL
    )""",
)


def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _user(row: sqlite3.Row) -> User:
    return User(
        user_id=row["user_id"],
        username=row["username"],
        role=Role(row["role"]),
        password_hash=row["password_hash"],
        must_change_password=bool(row["must_change_password"]),
        disabled=bool(row["disabled"]),
        created_utc=_dt(row["created_utc"]),
    )


class AccountStore:
    def __init__(self, path: Path) -> None:
        self._lock = threading.RLock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise PersistenceError(
                f"the account database could not be opened at {path.name}: {exc}",
                context={"path": path.name},
            ) from exc
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            for statement in _SCHEMA:
                self._db.execute(statement)
            self._db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Everything in the block commits together or not at all."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def query(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, params))

    def run(self, sql: str, params: tuple[object, ...] = ()) -> int:
        with self._lock:
            return self._db.execute(sql, params).rowcount

    def integrity_ok(self) -> bool:
        return bool(self.query("PRAGMA integrity_check")[0][0] == "ok")

    # ------------------------------------------------------------------ users

    def add_user(self, user: User) -> None:
        self.run(
            "INSERT INTO users (user_id, username, role, password_hash, must_change_password,"
            " disabled, created_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user.user_id,
                user.username,
                user.role.value,
                user.password_hash,
                int(user.must_change_password),
                int(user.disabled),
                _ts(user.created_utc),
            ),
        )

    def user_by_name(self, username: str) -> User | None:
        rows = self.query(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
        )
        return _user(rows[0]) if rows else None

    def user_by_id(self, user_id: str) -> User | None:
        rows = self.query("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return _user(rows[0]) if rows else None

    def users(self) -> list[User]:
        rows = self.query("SELECT * FROM users ORDER BY username COLLATE NOCASE")
        return [_user(r) for r in rows]

    def count_administrators(self, *, enabled_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM users WHERE role = ?"
        if enabled_only:
            sql += " AND disabled = 0"
        return int(self.query(sql, (Role.ADMINISTRATOR.value,))[0][0])

    def update_user(
        self,
        user_id: str,
        *,
        role: Role | None = None,
        password_hash: str | None = None,
        must_change_password: bool | None = None,
        disabled: bool | None = None,
    ) -> bool:
        """One fixed statement per column, in one transaction: no SQL is assembled from input."""
        changed = False
        with self.transaction() as db:
            if role is not None:
                db.execute("UPDATE users SET role = ? WHERE user_id = ?", (role.value, user_id))
                changed = True
            if password_hash is not None:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE user_id = ?", (password_hash, user_id)
                )
                changed = True
            if must_change_password is not None:
                db.execute(
                    "UPDATE users SET must_change_password = ? WHERE user_id = ?",
                    (int(must_change_password), user_id),
                )
                changed = True
            if disabled is not None:
                db.execute(
                    "UPDATE users SET disabled = ? WHERE user_id = ?", (int(disabled), user_id)
                )
                changed = True
        return changed and self.user_by_id(user_id) is not None

    def delete_user(self, user_id: str) -> bool:
        return self.run("DELETE FROM users WHERE user_id = ?", (user_id,)) > 0

    def delete_administrators(self) -> int:
        return self.run("DELETE FROM users WHERE role = ?", (Role.ADMINISTRATOR.value,))

    # ------------------------------------------------------------------ sessions

    def add_session(self, s: SessionRecord) -> None:
        self.run(
            "INSERT INTO sessions (token_hash, user_id, created_utc, last_seen_utc, expires_utc,"
            " locked) VALUES (?, ?, ?, ?, ?, ?)",
            (
                s.token_hash,
                s.user_id,
                _ts(s.created_utc),
                _ts(s.last_seen_utc),
                _ts(s.expires_utc),
                int(s.locked),
            ),
        )

    def session(self, token_hash: str) -> SessionRecord | None:
        rows = self.query("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))
        if not rows:
            return None
        r = rows[0]
        return SessionRecord(
            token_hash=r["token_hash"],
            user_id=r["user_id"],
            created_utc=_dt(r["created_utc"]),
            last_seen_utc=_dt(r["last_seen_utc"]),
            expires_utc=_dt(r["expires_utc"]),
            locked=bool(r["locked"]),
        )

    def touch_session(self, token_hash: str, at: datetime) -> None:
        self.run(
            "UPDATE sessions SET last_seen_utc = ? WHERE token_hash = ?", (_ts(at), token_hash)
        )

    def set_session_locked(self, token_hash: str, locked: bool) -> None:
        self.run("UPDATE sessions SET locked = ? WHERE token_hash = ?", (int(locked), token_hash))

    def delete_session(self, token_hash: str) -> bool:
        return self.run("DELETE FROM sessions WHERE token_hash = ?", (token_hash,)) > 0

    def delete_user_sessions(self, user_id: str) -> int:
        return self.run("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def delete_all_sessions(self) -> int:
        return self.run("DELETE FROM sessions")

    def delete_expired_sessions(self, *, absolute_before: datetime, idle_before: datetime) -> int:
        return self.run(
            "DELETE FROM sessions WHERE expires_utc <= ? OR last_seen_utc <= ?",
            (_ts(absolute_before), _ts(idle_before)),
        )

    # ------------------------------------------------------------------ recovery codes

    def replace_recovery_codes(self, user_id: str, hashes: list[str]) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
            db.executemany(
                "INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
                [(user_id, h) for h in hashes],
            )

    def unused_recovery_codes(self, user_id: str) -> list[tuple[int, str]]:
        rows = self.query(
            "SELECT code_id, code_hash FROM recovery_codes WHERE user_id = ? AND used_utc IS NULL",
            (user_id,),
        )
        return [(int(r["code_id"]), str(r["code_hash"])) for r in rows]

    def consume_recovery_code(self, code_id: int, at: datetime) -> bool:
        changed = self.run(
            "UPDATE recovery_codes SET used_utc = ? WHERE code_id = ? AND used_utc IS NULL",
            (_ts(at), code_id),
        )
        return changed > 0

    # ------------------------------------------------------------------ lockouts

    def lockout(self, key: str) -> Lockout | None:
        rows = self.query("SELECT * FROM lockouts WHERE key = ?", (key,))
        if not rows:
            return None
        r = rows[0]
        until = r["locked_until_utc"]
        return Lockout(
            key=r["key"],
            failures=int(r["failures"]),
            locked_until_utc=None if until is None else _dt(until),
            last_failure_utc=_dt(r["last_failure_utc"]),
        )

    def save_lockout(self, lock: Lockout) -> None:
        self.run(
            "INSERT INTO lockouts (key, failures, locked_until_utc, last_failure_utc)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET failures = excluded.failures,"
            " locked_until_utc = excluded.locked_until_utc,"
            " last_failure_utc = excluded.last_failure_utc",
            (lock.key, lock.failures, _ts(lock.locked_until_utc), _ts(lock.last_failure_utc)),
        )

    def clear_lockout(self, key: str) -> None:
        self.run("DELETE FROM lockouts WHERE key = ?", (key,))

    def prune_lockouts(self, *, idle_before: datetime, now: datetime) -> int:
        """Forget keys quiet for a day and no longer locked, so an attacker submitting endless
        random usernames cannot grow this table without bound."""
        return self.run(
            "DELETE FROM lockouts WHERE last_failure_utc <= ?"
            " AND (locked_until_utc IS NULL OR locked_until_utc <= ?)",
            (_ts(idle_before), _ts(now)),
        )

    # ------------------------------------------------------------------ meta

    def meta(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return None if not rows else str(rows[0][0])

    def set_meta(self, key: str, value: str) -> None:
        self.run(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value ="
            " excluded.value",
            (key, value),
        )
