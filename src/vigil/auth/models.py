"""Plain records for the account store. Frozen: a caller changes the store, not a copy of a row."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vigil.auth.roles import Permission, Role, can


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    username: str
    role: Role
    password_hash: str
    must_change_password: bool
    disabled: bool
    created_utc: datetime


@dataclass(frozen=True, slots=True)
class SessionRecord:
    token_hash: str
    user_id: str
    created_utc: datetime
    last_seen_utc: datetime
    expires_utc: datetime  # the absolute ceiling; idle expiry is computed from last_seen_utc
    locked: bool


@dataclass(frozen=True, slots=True)
class Lockout:
    key: str
    failures: int
    locked_until_utc: datetime | None
    last_failure_utc: datetime


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting. `via` says how: a session cookie, or the configured API token."""

    user_id: str
    username: str
    role: Role
    via: str  # "session" | "token"
    locked: bool = False
    token_hash: str | None = None
    must_change_password: bool = False

    def can(self, permission: Permission) -> bool:
        return can(self.role, permission)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    at_utc: datetime
    user_id: str | None
    username: str | None
    action: str
    target: str | None
    detail: str
    prev_hash: str
    row_hash: str
