"""Secret detection and redaction shared by config validation and logging."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

REDACTED = "***"
# Above this many bytes a bytes-like value is summarised rather than rendered. Small blobs
# (a hash, a nonce, a short header) stay readable because they are useful in a log line.
MAX_INLINE_BYTES = 256

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


def summarise_opaque(value: object) -> str | None:
    """A short description of a value that must never be rendered into a log line, or None.

    Two kinds qualify, and both mean "image data":

      * **a bytes-like blob above `MAX_INLINE_BYTES`** — an encoded JPEG;
      * **an array-like with a `shape`** — a decoded frame, or a `PixelBuffer` wrapping one.

    01 §10 says no image data leaves the process in a log, and a single 1080p frame is 6.2 MB
    of it, so the operational argument agrees with the privacy one. Until P6 that held only
    because no caller happened to log one — the same "statement about today's callers" that
    `response/paths.py` rightly distrusts, and the caller that matters is the one added next
    year. Duck-typed rather than imported: `core/` must not depend on numpy or OpenCV
    (the `domain-purity` import contract).
    """
    if isinstance(value, bytes | bytearray | memoryview):
        size = len(bytes(value)) if isinstance(value, memoryview) else len(value)
        return None if size <= MAX_INLINE_BYTES else f"<{type(value).__name__}: {size} bytes>"
    shape = getattr(value, "shape", None)
    if shape is not None and not isinstance(shape, type):
        return f"<{type(value).__name__} shape={shape!r}>"
    if hasattr(value, "__array_interface__") or hasattr(value, "__cuda_array_interface__"):
        return f"<{type(value).__name__} array>"
    return None


def redact_value(value: object, *, key: str | None = None) -> object:
    """Recursively redact a structure destined for a log line."""
    if key is not None and is_sensitive_key(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    opaque = summarise_opaque(value)
    if opaque is not None:
        return opaque
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [redact_value(v) for v in value]
    return value
