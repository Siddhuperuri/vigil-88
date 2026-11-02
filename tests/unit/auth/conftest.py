"""Shared fixtures: a real SQLite account store on a manual clock, with cheap Argon2 parameters."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from vigil.auth.audit import AuditLog
from vigil.auth.models import Principal
from vigil.auth.passwords import HashParams, PasswordHasherService
from vigil.auth.roles import Role
from vigil.auth.service import AuthService, SessionPolicy
from vigil.auth.store import AccountStore
from vigil.core.clock import ManualClock

CHEAP = HashParams(memory_kib=8, iterations=1, parallelism=1)
GOOD = "correct horse battery"
SETUP_TOKEN = "setup-token-for-tests"


@dataclass
class Rig:
    service: AuthService
    store: AccountStore
    audit: AuditLog
    clock: ManualClock
    hasher: PasswordHasherService

    def advance(self, seconds: float) -> None:
        self.clock.advance_ms(seconds * 1000)

    def admin(self, name: str = "alice") -> tuple[Principal, str]:
        """Complete setup and log in; returns the principal and the session token."""
        self.service.setup(token=SETUP_TOKEN, username=name, password=GOOD)
        result = self.service.login(name, GOOD)
        return result.principal, result.token

    def user(self, admin: Principal, name: str, role: Role = Role.OPERATOR) -> str:
        return self.service.create_user(
            admin, username=name, password=GOOD + "!", role=role
        ).user_id


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    clock = ManualClock()
    store = AccountStore(tmp_path / "auth.sqlite3")
    audit = AuditLog(store, clock)
    hasher = PasswordHasherService(CHEAP)
    service = AuthService(
        store,
        audit,
        hasher,
        clock,
        policy=SessionPolicy(idle_timeout_s=900, absolute_lifetime_s=12 * 3600),
        setup_token=SETUP_TOKEN,
    )
    yield Rig(service, store, audit, clock, hasher)
    store.close()
