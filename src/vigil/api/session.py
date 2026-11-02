"""Who may call what (ADR 0016): sessions, permissions and request discipline.

Every route declares **exactly one** requirement with `requires(...)`. A requirement is a FastAPI
security dependency, so it appears in the OpenAPI document as a scope on the operation, and a test
reads that document and fails the build when a route has none or has two. A route that is meant to
be open (sign-in, setup, status) is named in `PUBLIC_ROUTES`; nothing else may be.

Two guards sit behind the same `requires(...)`:

  * `AccountsGuard` — cookie sessions, roles, lock, origin checks. Used by `vigil serve`.
  * `LegacyGuard` — the optional shared bearer token and nothing else, unchanged from before
    accounts existed, so `vigil run --serve` and every existing test behave as they did.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import Depends, HTTPException, Request, Response, Security, status
from fastapi.security import APIKeyCookie, SecurityScopes
from pydantic import SecretStr

from vigil.api.security import bearer_matches
from vigil.auth.models import Principal
from vigil.auth.roles import Permission, Role, least_role_for
from vigil.auth.service import AuthService

SESSION_COOKIE = "vigil_session"
REQUEST_HEADER = "x-vigil-request"  # a custom header a cross-site form cannot set
SESSION_SCOPE = "session"  # any signed-in user, even with the console locked
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
PROGRAMMATIC_ACTOR = "api-client"  # who the audit trail names for the shared-bearer client

# Routes that are open on purpose. The permission test fails on anything else without a scope.
PUBLIC_ROUTES = frozenset(
    {
        ("GET", "/api/v1/auth/status"),
        ("POST", "/api/v1/auth/setup"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/recover"),
    }
)

_cookie_scheme = APIKeyCookie(name=SESSION_COOKIE, scheme_name="vigilSession", auto_error=False)


class Guard(Protocol):
    def discipline(self, request: Request) -> None: ...

    def check(
        self, request: Request, scopes: list[str], cookie: str | None
    ) -> Principal | None: ...


def _host_name(host_header: str | None) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [::1]:8088
        return host.split("]")[0] + "]"
    return host.split(":")[0]


class LegacyGuard:
    """The shared bearer token, if enabled. Permissions are not consulted: there are no roles."""

    def __init__(self, *, enabled: bool, token: SecretStr | None) -> None:
        self._enabled = enabled
        self._expected = token.get_secret_value().encode() if token else b""

    def discipline(self, request: Request) -> None:
        return None

    def check(self, request: Request, scopes: list[str], cookie: str | None) -> Principal | None:
        if self._enabled and not bearer_matches(
            request.headers.get("authorization"), self._expected
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="a valid bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None


class AccountsGuard:
    def __init__(
        self,
        service: AuthService,
        *,
        bearer_enabled: bool,
        bearer_token: SecretStr | None,
        loopback_only: bool,
    ) -> None:
        self._service = service
        self._bearer = (
            bearer_token.get_secret_value().encode() if bearer_enabled and bearer_token else None
        )
        self._loopback_only = loopback_only

    def discipline(self, request: Request) -> None:
        """Host and Origin discipline (plan §5.3): a web page in an ordinary browser, or a DNS-
        rebinding trick, must not be able to drive the engine."""
        if self._loopback_only and _host_name(request.headers.get("host")) not in LOOPBACK_HOSTS:
            raise HTTPException(status_code=400, detail="unexpected Host header")
        if request.method in SAFE_METHODS or "authorization" in request.headers:
            return  # a bearer client carries no ambient credential, so there is nothing to forge
        if request.headers.get(REQUEST_HEADER) != "1":
            raise HTTPException(status_code=403, detail=f"missing the {REQUEST_HEADER} header")
        origin = request.headers.get("origin")
        if origin is not None:
            origin_host = _host_name(origin.split("://", 1)[-1])
            if origin_host != _host_name(request.headers.get("host")):
                raise HTTPException(status_code=403, detail="cross-origin request refused")

    def principal_for(self, request: Request, cookie: str | None) -> Principal | None:
        if cookie:
            found = self._service.authenticate(cookie)
            if found is not None:
                return found
        if self._bearer is not None and bearer_matches(
            request.headers.get("authorization"), self._bearer
        ):
            return Principal(PROGRAMMATIC_ACTOR, PROGRAMMATIC_ACTOR, Role.OPERATOR, via="token")
        return None

    def check(self, request: Request, scopes: list[str], cookie: str | None) -> Principal | None:
        if len(scopes) != 1:
            # A route with none is caught by the test; one with two is a wiring mistake. Either
            # way refuse loudly rather than guess which requirement was meant.
            raise HTTPException(status_code=500, detail="route has no single access requirement")
        scope = scopes[0]
        principal = self.principal_for(request, cookie)
        if principal is None:
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        request.state.principal = principal
        if scope == SESSION_SCOPE:
            return principal
        if principal.locked:
            raise HTTPException(
                status_code=423, detail="The console is locked. Unlock it to continue."
            )
        if principal.must_change_password:
            raise HTTPException(status_code=403, detail="Change your password before continuing.")
        permission = Permission(scope)
        if not principal.can(permission):
            raise HTTPException(
                status_code=403,
                detail=f"This needs the {least_role_for(permission).value} role.",
            )
        return principal


def _guard(request: Request) -> Guard:
    guard: Guard = request.app.state.guard
    return guard


def request_discipline(request: Request) -> None:
    """App-level dependency: runs for every route, including the public ones."""
    _guard(request).discipline(request)


def _enforce(
    request: Request,
    security_scopes: SecurityScopes,
    cookie: str | None = Depends(_cookie_scheme),
) -> Principal | None:
    return _guard(request).check(request, security_scopes.scopes, cookie)


def requires(permission: Permission | None) -> Any:
    """The one access requirement of a route. `None` means "any signed-in user, even locked"."""
    return Security(_enforce, scopes=[SESSION_SCOPE if permission is None else permission.value])


# ---------------------------------------------------------------- what handlers need


def actor_name(request: Request) -> str:
    """The name to record as the actor. Without an account layer it is the historical default."""
    principal: Principal | None = getattr(request.state, "principal", None)
    return principal.username if principal is not None else "operator"


def current_principal(request: Request) -> Principal:
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return principal


def audit_event(
    request: Request,
    action: str,
    *,
    target: str | None = None,
    detail: dict[str, object] | None = None,
) -> None:
    """Record a user action in the audit chain, when accounts are on. A failure to write the row
    propagates (a 500): the action happened, and an unrecorded one must not look like success."""
    service: AuthService | None = request.app.state.auth
    if service is None:
        return
    service.audit.record(
        action, actor=getattr(request.state, "principal", None), target=target, detail=detail
    )


# ---------------------------------------------------------------- the cookie


def set_session_cookie(response: Response, token: str) -> None:
    """`HttpOnly` so script cannot read it, `SameSite=Strict` so another site cannot send it. No
    `Secure` flag yet: whether Chromium accepts one for the loopback origin is a D0 check."""
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", path="/", secure=False
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
