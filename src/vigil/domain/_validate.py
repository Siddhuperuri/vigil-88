"""Small construction-time checks shared by domain types. Stdlib only."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TypeVar

K = TypeVar("K")
V = TypeVar("V")


def require_ratio(name: str, value: float) -> None:
    if not (math.isfinite(value) and 0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be a finite value in [0, 1], got {value!r}")


def require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")


def require_non_negative(name: str, value: float) -> None:
    if not (math.isfinite(value) and value >= 0):
        raise ValueError(f"{name} must be >= 0, got {value!r}")


def require_positive(name: str, value: float) -> None:
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be > 0, got {value!r}")


def require_non_empty(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def require_utc(name: str, value: datetime) -> None:
    """Wall timestamps are timezone-aware and stored in UTC (05 §1 rule 4)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(None):
        raise ValueError(f"{name} must be in UTC, got offset {value.utcoffset()}")


def frozen_mapping(value: Mapping[K, V]) -> Mapping[K, V]:
    """A read-only copy, so a frozen dataclass is genuinely immutable all the way down."""
    return MappingProxyType(dict(value))
