"""Secret detection and redaction shared by config validation and logging."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

REDACTED = "***"

_SENSITIVE_KEY_RE = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|credential|private[_-]?key)",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/\s@]+)@")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(pass(?:word|wd)?|secret|token|api[_-]?key)(\s*[=:]\s*)([^\s,;&]+)"
)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(key))


def contains_inline_credentials(url: str) -> bool:
    """True when a URL carries userinfo: a username, optionally with a password."""
    return bool(_URL_USERINFO_RE.search(url))


def redact_text(text: str) -> str:
    text = _URL_USERINFO_RE.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def redact_value(value: object, *, key: str | None = None) -> object:
    """Recursively redact a structure destined for a log line."""
    if key is not None and is_sensitive_key(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [redact_value(v) for v in value]
    return value
