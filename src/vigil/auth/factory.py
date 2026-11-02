"""Build the account service from settings, in one place so `serve` and tests agree."""

from __future__ import annotations

from vigil.auth.audit import AuditLog
from vigil.auth.passwords import PasswordHasherService
from vigil.auth.service import AuthService, SessionPolicy
from vigil.auth.store import AccountStore
from vigil.config.paths import ResolvedPaths
from vigil.config.settings import Settings
from vigil.core.clock import Clock

ACCOUNTS_FILE = "auth.sqlite3"


def build_auth_service(
    settings: Settings, paths: ResolvedPaths, clock: Clock, *, setup_token: str | None = None
) -> tuple[AuthService, AccountStore]:
    """The caller owns the store and closes it after the API has stopped."""
    store = AccountStore(paths.db_dir / ACCOUNTS_FILE)
    cfg = settings.api.accounts
    service = AuthService(
        store,
        AuditLog(store, clock),
        PasswordHasherService(),
        clock,
        policy=SessionPolicy(
            idle_timeout_s=cfg.idle_timeout_minutes * 60,
            absolute_lifetime_s=cfg.absolute_lifetime_hours * 3600,
        ),
        setup_token=setup_token,
    )
    return service, store
