"""Sign-in, sessions, users and the audit log (ADR 0016).

Only four routes are open (`PUBLIC_ROUTES`): status, setup, login and recover. Everything else
declares a requirement, and the permission test reads the OpenAPI document to prove it.
"""

from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from vigil.api import session as S
from vigil.auth import errors as E
from vigil.auth.models import AuditEntry, Principal, User
from vigil.auth.roles import Permission, Role
from vigil.auth.service import AuthService

_STATUS = {
    E.INVALID_CREDENTIALS: status.HTTP_401_UNAUTHORIZED,
    E.INVALID_CODE: status.HTTP_401_UNAUTHORIZED,
    E.INVALID_SESSION: status.HTTP_401_UNAUTHORIZED,
    E.LOCKED_OUT: status.HTTP_429_TOO_MANY_REQUESTS,
    E.SETUP_NOT_ALLOWED: status.HTTP_403_FORBIDDEN,
    E.POLICY_VIOLATION: status.HTTP_422_UNPROCESSABLE_CONTENT,
    E.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    E.CONFLICT: status.HTTP_409_CONFLICT,
}
_SECRET = Field(min_length=1, max_length=256)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(_Body):
    username: str = Field(min_length=1, max_length=64)
    password: str = _SECRET


class SetupIn(LoginIn):
    token: str = Field(min_length=1, max_length=128)


class PasswordIn(_Body):
    current: str = _SECRET
    new: str = _SECRET


class UnlockIn(_Body):
    password: str = _SECRET


class RecoverIn(_Body):
    username: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=64)
    new_password: str = _SECRET


class NewUserIn(LoginIn):
    role: Role


class UserPatch(_Body):
    role: Role | None = None
    disabled: bool | None = None


class ResetIn(_Body):
    new_password: str = _SECRET


class UserOut(BaseModel):
    user_id: str
    username: str
    role: Role
    must_change_password: bool
    disabled: bool

    @classmethod
    def of(cls, u: User | Principal) -> UserOut:
        return cls(
            user_id=u.user_id,
            username=u.username,
            role=u.role,
            must_change_password=u.must_change_password,
            disabled=getattr(u, "disabled", False),
        )


class StatusOut(BaseModel):
    accounts_enabled: bool = True
    setup_required: bool
    authenticated: bool
    locked: bool = False
    user: UserOut | None = None


class SessionOut(BaseModel):
    user: UserOut
    idle_timeout_s: int
    locked: bool = False


class SetupOut(SessionOut):
    recovery_codes: list[str]


class AuditOut(BaseModel):
    seq: int
    at_utc: str
    user_id: str | None
    username: str | None
    action: str
    target: str | None
    detail: str

    @classmethod
    def of(cls, e: AuditEntry) -> AuditOut:
        return cls(
            seq=e.seq,
            at_utc=e.at_utc.isoformat(),
            user_id=e.user_id,
            username=e.username,
            action=e.action,
            target=e.target,
            detail=e.detail,
        )


def _refuse(exc: E.AuthError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after_s)} if exc.retry_after_s else None
    return HTTPException(
        status_code=_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
        headers=headers,
    )


def build_router(service: AuthService) -> APIRouter:
    router = APIRouter(tags=["accounts"])

    # ------------------------------------------------------------ open routes

    @router.get("/auth/status", response_model=StatusOut)
    def auth_status(request: Request) -> StatusOut:
        """What the console needs before it can draw anything: is setup pending, and who is this."""
        cookie = request.cookies.get(S.SESSION_COOKIE)
        who = service.authenticate(cookie) if cookie else None
        return StatusOut(
            setup_required=service.setup_required(),
            authenticated=who is not None,
            locked=bool(who and who.locked),
            user=None if who is None else UserOut.of(who),
        )

    @router.post("/auth/setup", response_model=SetupOut, status_code=status.HTTP_201_CREATED)
    def setup(body: Annotated[SetupIn, Body()], response: Response) -> SetupOut:
        try:
            user, codes = service.setup(
                token=body.token, username=body.username, password=body.password
            )
            result = service.login(body.username, body.password)
        except E.AuthError as exc:
            raise _refuse(exc) from None
        S.set_session_cookie(response, result.token)
        return SetupOut(
            user=UserOut.of(user), idle_timeout_s=result.idle_timeout_s, recovery_codes=codes
        )

    @router.post("/auth/login", response_model=SessionOut)
    def login(body: Annotated[LoginIn, Body()], response: Response) -> SessionOut:
        try:
            result = service.login(body.username, body.password)
        except E.AuthError as exc:
            raise _refuse(exc) from None
        S.set_session_cookie(response, result.token)
        return SessionOut(user=UserOut.of(result.principal), idle_timeout_s=result.idle_timeout_s)

    @router.post("/auth/recover", status_code=status.HTTP_204_NO_CONTENT)
    def recover(body: Annotated[RecoverIn, Body()]) -> Response:
        try:
            service.recover(body.username, body.code, body.new_password)
        except E.AuthError as exc:
            raise _refuse(exc) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------ any signed-in user

    session = S.requires(None)

    @router.get("/auth/me", response_model=SessionOut, dependencies=[session])
    def me(request: Request) -> SessionOut:
        who = S.current_principal(request)
        return SessionOut(
            user=UserOut.of(who), idle_timeout_s=service.policy.idle_timeout_s, locked=who.locked
        )

    @router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[session])
    def logout(request: Request, response: Response) -> Response:
        service.logout(S.current_principal(request))
        out = Response(status_code=status.HTTP_204_NO_CONTENT)
        S.clear_session_cookie(out)
        return out

    @router.post("/auth/lock", status_code=status.HTTP_204_NO_CONTENT, dependencies=[session])
    def lock(request: Request) -> Response:
        service.lock(S.current_principal(request))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/auth/unlock", status_code=status.HTTP_204_NO_CONTENT, dependencies=[session])
    def unlock(request: Request, body: Annotated[UnlockIn, Body()]) -> Response:
        try:
            service.unlock(S.current_principal(request), body.password)
        except E.AuthError as exc:
            raise _refuse(exc) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT, dependencies=[session])
    def change_password(request: Request, body: Annotated[PasswordIn, Body()]) -> Response:
        try:
            service.change_password(S.current_principal(request), body.current, body.new)
        except E.AuthError as exc:
            raise _refuse(exc) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/auth/recovery-codes", response_model=list[str], dependencies=[session])
    def new_recovery_codes(request: Request, body: Annotated[UnlockIn, Body()]) -> list[str]:
        """Replace the recovery codes. Asks for the password again: codes are a credential."""
        who = S.current_principal(request)
        try:
            service.unlock(who, body.password)  # re-verifies, and counts failures like a login
            return service.regenerate_recovery_codes(who)
        except E.AuthError as exc:
            raise _refuse(exc) from None

    # ------------------------------------------------------------ administrators

    manage = S.requires(Permission.MANAGE_USERS)

    @router.get("/users", response_model=list[UserOut], dependencies=[manage])
    def users() -> list[UserOut]:
        return [UserOut.of(u) for u in service.list_users()]

    @router.post(
        "/users", response_model=UserOut, status_code=status.HTTP_201_CREATED, dependencies=[manage]
    )
    def create_user(request: Request, body: Annotated[NewUserIn, Body()]) -> UserOut:
        try:
            made = service.create_user(
                S.current_principal(request),
                username=body.username,
                password=body.password,
                role=body.role,
            )
        except E.AuthError as exc:
            raise _refuse(exc) from None
        return UserOut.of(made)

    @router.patch("/users/{user_id}", response_model=UserOut, dependencies=[manage])
    def patch_user(request: Request, user_id: str, body: Annotated[UserPatch, Body()]) -> UserOut:
        actor = S.current_principal(request)
        try:
            out: User | None = None
            if body.role is not None:
                out = service.set_role(actor, user_id, body.role)
            if body.disabled is not None:
                out = service.set_disabled(actor, user_id, body.disabled)
        except E.AuthError as exc:
            raise _refuse(exc) from None
        if out is None:
            raise HTTPException(status_code=422, detail="Nothing to change.")
        return UserOut.of(out)

    @router.delete(
        "/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[manage]
    )
    def delete_user(request: Request, user_id: str) -> Response:
        try:
            service.delete_user(S.current_principal(request), user_id)
        except E.AuthError as exc:
            raise _refuse(exc) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/users/{user_id}/reset-password", response_model=UserOut, dependencies=[manage])
    def reset_password(request: Request, user_id: str, body: Annotated[ResetIn, Body()]) -> UserOut:
        try:
            return UserOut.of(
                service.reset_password(S.current_principal(request), user_id, body.new_password)
            )
        except E.AuthError as exc:
            raise _refuse(exc) from None

    # ------------------------------------------------------------ audit log

    view_audit = S.requires(Permission.VIEW_AUDIT)

    @router.get("/audit", response_model=list[AuditOut], dependencies=[view_audit])
    def audit(
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        before_seq: Annotated[int | None, Query(ge=1)] = None,
        action: Annotated[str | None, Query(max_length=64)] = None,
    ) -> list[AuditOut]:
        entries = service.audit.entries(limit=limit, before_seq=before_seq, action=action)
        return [AuditOut.of(e) for e in entries]

    @router.get("/audit/verify", dependencies=[view_audit])
    def verify_audit() -> dict[str, object]:
        report = service.audit.verify()
        return {"ok": report.ok, "rows": report.rows, "first_bad_seq": report.first_bad_seq}

    @router.get("/audit/export", dependencies=[view_audit], response_class=PlainTextResponse)
    def export_audit(
        request: Request, fmt: Annotated[str, Query(alias="format", pattern="^(csv|json)$")] = "csv"
    ) -> Response:
        entries = service.audit.entries(limit=1000)
        S.audit_event(request, "audit.exported", detail={"format": fmt, "rows": len(entries)})
        if fmt == "json":
            body = "[" + ",".join(AuditOut.of(e).model_dump_json() for e in entries) + "]"
            return Response(body, media_type="application/json")
        buf = io.StringIO()
        out = csv.writer(buf)
        out.writerow(["seq", "at_utc", "user", "action", "target", "detail"])
        for e in entries:
            # A cell starting with a formula character is neutralised, as in analytics exports.
            cells = [e.username or "", e.action, e.target or "", e.detail]
            safe = ["'" + c if c[:1] in ("=", "+", "-", "@", "\t", "\r") else c for c in cells]
            out.writerow([e.seq, e.at_utc.isoformat(), *safe])
        return Response(buf.getvalue(), media_type="text/csv")

    return router
