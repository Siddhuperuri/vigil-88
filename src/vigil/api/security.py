"""Optional bearer-token authentication (01 §10).

The API binds loopback by default and the config validator refuses a non-loopback bind
without auth enabled and a token of at least 32 characters. When auth is on, every API route
requires `Authorization: Bearer <token>`; the comparison is constant-time.

Limitation (docs/LIMITATIONS.md): a browser `<img>` cannot send headers and tokens must never
go in URLs, so with auth enabled the MJPEG stream works for programmatic clients but not for a
bare `<img>` tag. The session/cookie flow that fixes that belongs to the full console.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastapi import Header, HTTPException, status
from pydantic import SecretStr


def make_auth_dependency(enabled: bool, token: SecretStr | None) -> Callable[..., None]:
    expected = token.get_secret_value().encode() if token else b""

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if not enabled:
            return
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="a valid bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_auth
