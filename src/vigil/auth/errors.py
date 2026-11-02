"""Authentication failures. One class with a machine-readable code, so the API can map each to a
status without parsing messages, and no message ever says *which half* of a credential was wrong."""

from __future__ import annotations

from collections.abc import Mapping

from vigil.core.errors import VigilError

INVALID_CREDENTIALS = "invalid_credentials"
LOCKED_OUT = "locked_out"
SETUP_NOT_ALLOWED = "setup_not_allowed"
POLICY_VIOLATION = "policy_violation"
NOT_FOUND = "not_found"
CONFLICT = "conflict"
INVALID_SESSION = "invalid_session"
INVALID_CODE = "invalid_code"

# Deliberately identical for an unknown user and a wrong password (07-style rule, plan §7.4).
GENERIC_FAILURE = "That username and password do not match."


class AuthError(VigilError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_s: int | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.code = code
        self.retry_after_s = retry_after_s
