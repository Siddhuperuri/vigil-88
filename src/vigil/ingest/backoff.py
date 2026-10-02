"""Reconnect backoff (07 §5): exponential with jitter, so cameras behind one device do not
retry in lockstep and hammer it after a network blip."""

from __future__ import annotations

import math
import random


def reconnect_delay_ms(
    attempt: int,
    *,
    base_ms: float,
    max_ms: float,
    jitter_ratio: float,
    rng: random.Random,
) -> float:
    """`attempt` is 0-based. Result is jittered by +/- `jitter_ratio` and never exceeds `max_ms`."""
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    capped = min(math.ldexp(base_ms, min(attempt, 62)), max_ms)
    jittered = capped * (1.0 + rng.uniform(-jitter_ratio, jitter_ratio))
    return max(0.0, min(jittered, max_ms))
