"""Camera credentials, resolved at connection time and never stored in a camera file (01 §10).

Two places a secret may live, tried in this order:

  1. **Windows Credential Manager** through `keyring`, named by `credential_ref` in the camera
     file. The camera file holds only that name. This is what the console writes.
  2. **Environment variables**, named by `username_env` and `password_env`. Kept for headless and
     scripted installs.

The resolved pair exists only in memory, only while a connection is being opened, and is never
logged: `redact_url` is the one function allowed to print a URL that might have carried it.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from vigil.core.errors import SourceError

SERVICE = "vigil-88"
REF_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}$")
INSTALL_HINT = "install the credentials extra: uv sync --extra credentials"

KeyringGet = Callable[[str, str], str | None]


@dataclass(frozen=True, slots=True)
class Credentials:
    username: str
    password: str = ""

    def __repr__(self) -> str:  # a stray f-string or log line must not print the secret
        return "Credentials(username=<set>, password=<hidden>)"


def _keyring_get(service: str, name: str) -> str | None:
    try:
        import keyring
    except ImportError as exc:
        raise SourceError(
            f"this camera's credentials are in Windows Credential Manager, but the keyring package "
            f"is not installed: {INSTALL_HINT}",
            retryable=False,
        ) from exc
    value: str | None = keyring.get_password(service, name)
    return value


def store_credentials(ref: str, username: str, password: str) -> None:
    """Write a pair to the Credential Manager. The caller has already authorised the request."""
    if not REF_PATTERN.match(ref):
        raise ValueError("a credential name is a short slug such as 'gate-camera'")
    try:
        import keyring
    except ImportError as exc:
        raise SourceError(f"keyring is not installed: {INSTALL_HINT}", retryable=False) from exc
    keyring.set_password(SERVICE, ref, json.dumps({"u": username, "p": password}))


def delete_credentials(ref: str) -> bool:
    try:
        import keyring
        from keyring.errors import PasswordDeleteError
    except ImportError as exc:
        raise SourceError(f"keyring is not installed: {INSTALL_HINT}", retryable=False) from exc
    try:
        keyring.delete_password(SERVICE, ref)
    except PasswordDeleteError:
        return False
    return True


def has_credentials(ref: str, *, get: KeyringGet = _keyring_get) -> bool:
    """Whether a pair is stored under `ref`. Never returns the pair."""
    return get(SERVICE, ref) is not None


def resolve_credentials(
    options: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
    get: KeyringGet = _keyring_get,
) -> Credentials | None:
    """The pair for a camera, or None when it is configured with none. A configured name that
    resolves to nothing is an error naming the *name* and never a value."""
    ref = options.get("credential_ref")
    if isinstance(ref, str) and ref:
        raw = get(SERVICE, ref)
        if raw is None:
            raise SourceError(
                f"no stored credential named {ref!r}: set it in the camera's settings",
                retryable=False,
            )
        try:
            data = json.loads(raw)
            return Credentials(str(data["u"]), str(data.get("p", "")))
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceError(
                f"the stored credential {ref!r} is unreadable: set it again", retryable=False
            ) from exc
    user_env, pass_env = options.get("username_env"), options.get("password_env")
    source = os.environ if env is None else env
    if isinstance(user_env, str) and user_env:
        user = source.get(user_env)
        if user is None:
            raise SourceError(
                f"the environment variable {user_env!r} (the camera's username) is not set",
                retryable=False,
            )
        password = source.get(pass_env, "") if isinstance(pass_env, str) and pass_env else ""
        return Credentials(user, password)
    return None


def with_credentials(url: str, creds: Credentials | None) -> str:
    """Insert a pair into an RTSP URL, percent-encoding it. The result is for the capture call
    only: it must not be logged, stored or put in an error message."""
    if creds is None:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # an IPv6 literal needs its brackets back
        host = f"[{host}]"
    netloc = f"{quote(creds.username, safe='')}:{quote(creds.password, safe='')}@{host}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def redact_url(url: str) -> str:
    """The URL with any user information removed, safe for an error message or a log."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port is not None else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))  # a query can carry a token too
