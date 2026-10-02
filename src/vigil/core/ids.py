"""Identifiers (01 §7.7): camera slugs, sortable ULIDs, operator-readable display refs."""

from __future__ import annotations

import random
import re
import threading
from datetime import UTC, datetime

from vigil.core.clock import Clock

CAMERA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,38}$")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RE = re.compile(rf"^[0-7][{_CROCKFORD}]{{25}}$")
_RANDOM_BITS = 80
_RANDOM_MASK = (1 << _RANDOM_BITS) - 1
_ULID_LEN = 26
_DISPLAY_SUFFIX_LEN = 6


def validate_camera_id(value: str) -> str:
    if not CAMERA_ID_PATTERN.match(value):
        raise ValueError(
            f"invalid camera_id {value!r}: must match {CAMERA_ID_PATTERN.pattern} "
            "(lowercase letters, digits, '_' and '-', 2-39 chars, starting alphanumeric)"
        )
    return value


def _encode(value: int) -> str:
    chars: list[str] = []
    for _ in range(_ULID_LEN):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def is_valid_ulid(value: str) -> bool:
    return bool(_ULID_RE.match(value))


def ulid_timestamp_ms(ulid: str) -> int:
    if not is_valid_ulid(ulid):
        raise ValueError(f"not a ULID: {ulid!r}")
    value = 0
    for char in ulid:
        value = (value << 5) | _CROCKFORD.index(char)
    return value >> _RANDOM_BITS


class UlidFactory:
    """Monotonic ULIDs: ids from one factory sort in creation order, even within a millisecond.

    Time comes from the injected Clock and randomness from an injected RNG, so tests are
    reproducible.
    """

    def __init__(self, clock: Clock, rng: random.Random) -> None:
        self._clock = clock
        self._rng = rng
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_random = 0

    def new(self) -> str:
        with self._lock:
            now_ms = int(self._clock.wall_utc().timestamp() * 1000)
            if now_ms <= self._last_ms:
                now_ms = self._last_ms
                self._last_random += 1
                if self._last_random > _RANDOM_MASK:
                    raise OverflowError("ULID randomness exhausted within one millisecond")
            else:
                self._last_random = self._rng.getrandbits(_RANDOM_BITS)
            self._last_ms = now_ms
            return _encode((now_ms << _RANDOM_BITS) | self._last_random)


def display_ref(incident_id: str, created_utc: datetime) -> str:
    """`INC-20261001-7F3A2B`: what an operator reads aloud."""
    if not is_valid_ulid(incident_id):
        raise ValueError(f"not a ULID: {incident_id!r}")
    day = created_utc.astimezone(UTC).strftime("%Y%m%d")
    return f"INC-{day}-{incident_id[-_DISPLAY_SUFFIX_LEN:]}"
