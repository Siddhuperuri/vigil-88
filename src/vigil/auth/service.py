"""Accounts, sessions and recovery: the one place authentication decisions are made (ADR 0016).

Everything time-dependent reads the injected `Clock`, so lockout windows, idle timeouts and absolute
lifetimes are tested by moving a fake clock, never by sleeping.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from vigil.auth import errors as E
from vigil.auth.audit import AuditLog
from vigil.auth.lockout import lock_seconds, username_key
from vigil.auth.models import Lockout, Principal, SessionRecord, User
from vigil.auth.passwords import PasswordHasherService, check_policy
from vigil.auth.roles import Role
from vigil.auth.store import AccountStore
from vigil.core.clock import Clock

RECOVERY_CODE_COUNT = 10
_TOUCH_EVERY_S = 10  # a busy console must not turn every request into a database write
_LOCKOUT_FORGET = timedelta(hours=24)
_USERNAME_MAX = 64


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    idle_timeout_s: int = 15 * 60
    absolute_lifetime_s: int = 12 * 3600


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str  # shown to the caller exactly once; only its SHA-256 is stored
    principal: Principal
    idle_timeout_s: int


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


def _normalise_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _format_code(raw: bytes) -> str:
    text = base64.b32encode(raw).decode("ascii")  # 10 bytes -> 16 characters, 80 bits
    return "-".join(text[i : i + 4] for i in range(0, 16, 4))


class AuthService:
    def __init__(
        self,
        store: AccountStore,
        audit: AuditLog,
        hasher: PasswordHasherService,
        clock: Clock,
        *,
        policy: SessionPolicy = SessionPolicy(),  # noqa: B008 - frozen value object
        setup_token: str | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._hasher = hasher
        self._clock = clock
        self._policy = policy
        # A token the supervisor supplies, or one made here and printed by the caller when run
        # headless. Held in memory only: a restart before setup simply issues another.
        self._setup_token: str | None = setup_token or secrets.token_urlsafe(24)
        self._generated_setup_token = setup_token is None

    # ------------------------------------------------------------------ status

    @property
    def audit(self) -> AuditLog:
        return self._audit

    @property
    def policy(self) -> SessionPolicy:
        return self._policy

    @property
    def setup_token_to_print(self) -> str | None:
        """The token, only when this service made it (so a headless run can show it once)."""
        return self._setup_token if self._generated_setup_token else None

    def setup_required(self) -> bool:
        return self._store.count_administrators(enabled_only=False) == 0

    # ------------------------------------------------------------------ setup

    def setup(self, *, token: str, username: str, password: str) -> tuple[User, list[str]]:
        """Create the first administrator. One shot: needs the setup token and no administrator."""
        if not self.setup_required() or self._setup_token is None:
            raise E.AuthError(E.SETUP_NOT_ALLOWED, "Setup is already complete.")
        if not hmac.compare_digest(token.encode(), self._setup_token.encode()):
            self._audit.record("setup.refused", detail={"reason": "bad setup token"})
            raise E.AuthError(E.SETUP_NOT_ALLOWED, "The setup token is not valid.")
        user = self._create(username, password, Role.ADMINISTRATOR, must_change=False)
        self._setup_token = None  # consumed
        codes = self._issue_recovery_codes(user.user_id)
        self._audit.record(
            "setup.completed", user_id=user.user_id, username=user.username, target=user.user_id
        )
        return user, codes

    def reissue_setup_token(self) -> str:
        """After `vigil auth reset-admin`: setup is required again and needs a fresh token."""
        self._setup_token = secrets.token_urlsafe(24)
        self._generated_setup_token = True
        return self._setup_token

    # ------------------------------------------------------------------ login and sessions

    def login(self, username: str, password: str) -> LoginResult:
        key = username_key(username)
        now = self._clock.wall_utc()
        self._store.prune_lockouts(idle_before=now - _LOCKOUT_FORGET, now=now)
        self._refuse_if_locked(key, username)
        user = self._store.user_by_name(username)
        ok = False
        if user is None or user.disabled:
            self._hasher.burn(password)  # same cost as a real check: timing reveals nothing
        else:
            ok = self._hasher.verify(user.password_hash, password)
        if not ok or user is None:
            self._record_failure(key, username)
            raise E.AuthError(E.INVALID_CREDENTIALS, E.GENERIC_FAILURE)
        self._store.clear_lockout(key)
        if self._hasher.needs_rehash(user.password_hash):
            self._store.update_user(user.user_id, password_hash=self._hasher.hash(password))
        result = self._open_session(user)
        self._audit.record("login.success", actor=result.principal, target=user.user_id)
        return result

    def authenticate(self, token: str) -> Principal | None:
        """Resolve a cookie to a principal, applying idle and absolute expiry. None if invalid."""
        if not token:
            return None
        digest = _hash_token(token)
        record = self._store.session(digest)
        if record is None:
            return None
        now = self._clock.wall_utc()
        idle_deadline = record.last_seen_utc + timedelta(seconds=self._policy.idle_timeout_s)
        if now >= record.expires_utc or now >= idle_deadline:
            self._store.delete_session(digest)
            self._audit.record(
                "session.expired", user_id=record.user_id, detail={"idle": now >= idle_deadline}
            )
            return None
        user = self._store.user_by_id(record.user_id)
        if user is None or user.disabled:
            self._store.delete_session(digest)
            return None
        if (now - record.last_seen_utc).total_seconds() >= _TOUCH_EVERY_S:
            self._store.touch_session(digest, now)
        return self._principal(user, digest, locked=record.locked)

    def logout(self, principal: Principal) -> None:
        if principal.token_hash:
            self._store.delete_session(principal.token_hash)
        self._audit.record("logout", actor=principal)

    def lock(self, principal: Principal) -> None:
        if principal.token_hash:
            self._store.set_session_locked(principal.token_hash, True)
        self._audit.record("session.locked", actor=principal)

    def unlock(self, principal: Principal, password: str) -> None:
        key = username_key(principal.username)
        self._refuse_if_locked(key, principal.username)
        user = self._store.user_by_id(principal.user_id)
        if user is None or not self._hasher.verify(user.password_hash, password):
            self._record_failure(key, principal.username)
            raise E.AuthError(E.INVALID_CREDENTIALS, "That password does not match.")
        self._store.clear_lockout(key)
        if principal.token_hash:
            self._store.set_session_locked(principal.token_hash, False)
        self._audit.record("session.unlocked", actor=principal)

    # ------------------------------------------------------------------ users

    def list_users(self) -> list[User]:
        return self._store.users()

    def create_user(self, actor: Principal, *, username: str, password: str, role: Role) -> User:
        user = self._create(username, password, role, must_change=True)
        self._audit.record(
            "user.created",
            actor=actor,
            target=user.user_id,
            detail={"username": user.username, "role": role.value},
        )
        return user

    def set_role(self, actor: Principal, user_id: str, role: Role) -> User:
        user = self._require_user(user_id)
        if user.role is Role.ADMINISTRATOR and role is not Role.ADMINISTRATOR:
            self._refuse_last_administrator(user)
        self._store.update_user(user_id, role=role)
        self._store.delete_user_sessions(user_id)  # a role change takes effect now, not at expiry
        self._audit.record(
            "user.role_changed",
            actor=actor,
            target=user_id,
            detail={"from": user.role.value, "to": role.value},
        )
        return self._require_user(user_id)

    def set_disabled(self, actor: Principal, user_id: str, disabled: bool) -> User:
        user = self._require_user(user_id)
        if disabled and user.role is Role.ADMINISTRATOR:
            self._refuse_last_administrator(user)
        self._store.update_user(user_id, disabled=disabled)
        if disabled:
            self._store.delete_user_sessions(user_id)
        self._audit.record(
            "user.disabled" if disabled else "user.enabled", actor=actor, target=user_id
        )
        return self._require_user(user_id)

    def delete_user(self, actor: Principal, user_id: str) -> None:
        user = self._require_user(user_id)
        if user.role is Role.ADMINISTRATOR:
            self._refuse_last_administrator(user)
        self._store.delete_user(user_id)
        self._audit.record(
            "user.deleted", actor=actor, target=user_id, detail={"username": user.username}
        )

    def reset_password(self, actor: Principal, user_id: str, new_password: str) -> User:
        """An administrator sets a password for someone else, who must change it at next login."""
        user = self._require_user(user_id)
        self._require_policy(new_password, user.username)
        self._store.update_user(
            user_id, password_hash=self._hasher.hash(new_password), must_change_password=True
        )
        self._store.delete_user_sessions(user_id)
        self._audit.record("user.password_reset", actor=actor, target=user_id)
        return self._require_user(user_id)

    def change_password(self, principal: Principal, current: str, new_password: str) -> None:
        user = self._require_user(principal.user_id)
        if not self._hasher.verify(user.password_hash, current):
            raise E.AuthError(E.INVALID_CREDENTIALS, "That password does not match.")
        self._require_policy(new_password, user.username)
        self._store.update_user(
            user.user_id,
            password_hash=self._hasher.hash(new_password),
            must_change_password=False,
        )
        # Every other session of this user ends; the one making the change stays.
        for other in self._sessions_of(user.user_id):
            if other != principal.token_hash:
                self._store.delete_session(other)
        self._audit.record("user.password_changed", actor=principal, target=user.user_id)

    # ------------------------------------------------------------------ recovery

    def regenerate_recovery_codes(self, principal: Principal) -> list[str]:
        codes = self._issue_recovery_codes(principal.user_id)
        self._audit.record("recovery.regenerated", actor=principal)
        return codes

    def recover(self, username: str, code: str, new_password: str) -> None:
        """Reset a password with a one-time recovery code. Spends attempts like a login does."""
        key = username_key(username)
        self._refuse_if_locked(key, username)
        user = self._store.user_by_name(username)
        matched: int | None = None
        if user is not None and not user.disabled:
            wanted = _normalise_code(code)
            for code_id, stored in self._store.unused_recovery_codes(user.user_id):
                if self._hasher.verify(stored, wanted):
                    matched = code_id
                    break
        else:
            self._hasher.burn(code)
        if user is None or matched is None:
            self._record_failure(key, username)
            raise E.AuthError(E.INVALID_CODE, "That username and recovery code do not match.")
        self._require_policy(new_password, user.username)
        if not self._store.consume_recovery_code(matched, self._clock.wall_utc()):
            raise E.AuthError(E.INVALID_CODE, "That username and recovery code do not match.")
        self._store.update_user(
            user.user_id,
            password_hash=self._hasher.hash(new_password),
            must_change_password=False,
        )
        self._store.delete_user_sessions(user.user_id)
        self._store.clear_lockout(key)
        self._audit.record("recovery.used", user_id=user.user_id, username=user.username)

    def reset_admin(self) -> str:
        """The last resort (`vigil auth reset-admin`): remove every administrator, end every
        session, require setup again. Incidents and evidence are untouched. Returns the fresh
        setup token the caller must show to the person at the keyboard."""
        removed = self._store.delete_administrators()
        self._store.delete_all_sessions()
        self._audit.record("admin.reset", detail={"administrators_removed": removed})
        return self.reissue_setup_token()

    # ------------------------------------------------------------------ internals

    def _create(self, username: str, password: str, role: Role, *, must_change: bool) -> User:
        name = username.strip()
        if (
            not name
            or len(name) > _USERNAME_MAX
            or not name.replace("-", "").replace("_", "").replace(".", "").isalnum()
        ):
            raise E.AuthError(
                E.POLICY_VIOLATION,
                "A username is 1 to 64 letters, digits, '-', '_' or '.'.",
            )
        self._require_policy(password, name)
        if self._store.user_by_name(name) is not None:
            raise E.AuthError(E.CONFLICT, "That username is already taken.")
        user = User(
            user_id=uuid.uuid4().hex,
            username=name,
            role=role,
            password_hash=self._hasher.hash(password),
            must_change_password=must_change,
            disabled=False,
            created_utc=self._clock.wall_utc(),
        )
        self._store.add_user(user)
        return user

    def _require_policy(self, password: str, username: str) -> None:
        problem = check_policy(password, username=username)
        if problem is not None:
            raise E.AuthError(E.POLICY_VIOLATION, problem)

    def _require_user(self, user_id: str) -> User:
        user = self._store.user_by_id(user_id)
        if user is None:
            raise E.AuthError(E.NOT_FOUND, "No such user.")
        return user

    def _refuse_last_administrator(self, user: User) -> None:
        if user.disabled or self._store.count_administrators() > 1:
            return
        raise E.AuthError(
            E.CONFLICT, "The last administrator cannot be removed, demoted or disabled."
        )

    def _sessions_of(self, user_id: str) -> list[str]:
        rows = self._store.query("SELECT token_hash FROM sessions WHERE user_id = ?", (user_id,))
        return [str(r[0]) for r in rows]

    def _open_session(self, user: User) -> LoginResult:
        token = secrets.token_urlsafe(32)  # 256 bits
        now = self._clock.wall_utc()
        self._store.add_session(
            SessionRecord(
                token_hash=_hash_token(token),
                user_id=user.user_id,
                created_utc=now,
                last_seen_utc=now,
                expires_utc=now + timedelta(seconds=self._policy.absolute_lifetime_s),
                locked=False,
            )
        )
        return LoginResult(
            token,
            self._principal(user, _hash_token(token), locked=False),
            self._policy.idle_timeout_s,
        )

    @staticmethod
    def _principal(user: User, token_hash: str, *, locked: bool) -> Principal:
        return Principal(
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            via="session",
            locked=locked,
            token_hash=token_hash,
            must_change_password=user.must_change_password,
        )

    def _issue_recovery_codes(self, user_id: str) -> list[str]:
        raws = [secrets.token_bytes(10) for _ in range(RECOVERY_CODE_COUNT)]
        codes = [_format_code(r) for r in raws]
        self._store.replace_recovery_codes(
            user_id, [self._hasher.hash(_normalise_code(c)) for c in codes]
        )
        return codes

    def _refuse_if_locked(self, key: str, submitted: str) -> None:
        lock = self._store.lockout(key)
        now = self._clock.wall_utc()
        if lock is not None and lock.locked_until_utc is not None and now < lock.locked_until_utc:
            remaining = max(1, int((lock.locked_until_utc - now).total_seconds() + 0.999))
            self._audit.record("login.locked_out", username=submitted.strip()[:_USERNAME_MAX])
            raise E.AuthError(
                E.LOCKED_OUT,
                f"Too many attempts. Try again in {remaining} s.",
                retry_after_s=remaining,
            )

    def _record_failure(self, key: str, submitted: str) -> None:
        now = self._clock.wall_utc()
        previous = self._store.lockout(key)
        failures = (previous.failures if previous else 0) + 1
        wait = lock_seconds(failures)
        self._store.save_lockout(
            Lockout(key, failures, now + timedelta(seconds=wait) if wait else None, now)
        )
        self._audit.record(
            "login.failure",
            username=submitted.strip()[:_USERNAME_MAX],
            detail={"consecutive_failures": failures, "locked_s": wait},
        )
