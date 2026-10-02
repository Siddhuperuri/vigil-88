"""Seeded randomness. Nothing in decision logic may use the global `random` state."""

from __future__ import annotations

import hashlib
import random


def seeded_rng(seed: int, stream: str = "") -> random.Random:
    """An independent, reproducible generator per (seed, stream) pair."""
    digest = hashlib.sha256(f"{seed}:{stream}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311 - not cryptographic
