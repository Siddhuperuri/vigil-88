"""Progressive lockout arithmetic (plan §7.4). Pure, so the schedule is a table in a test."""

from __future__ import annotations

THRESHOLD = 5  # consecutive failures before the first lock
BASE_LOCK_S = 30
MAX_LOCK_S = 900  # 15 minutes


def lock_seconds(consecutive_failures: int) -> int:
    """How long the key is locked *after* this many consecutive failures. 0 below the threshold."""
    if consecutive_failures < THRESHOLD:
        return 0
    return int(min(BASE_LOCK_S * 2 ** (consecutive_failures - THRESHOLD), MAX_LOCK_S))


def username_key(submitted: str) -> str:
    """The counter is keyed by what was typed, whether or not that account exists, so the lockout
    state itself reveals nothing about which usernames are real."""
    return submitted.strip().casefold()
