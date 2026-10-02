"""Unit conversion constants. Units are part of every name in this codebase (`_ms`, `_px`...)."""

from __future__ import annotations

NS_PER_MS: int = 1_000_000
MS_PER_S: int = 1_000
NS_PER_S: int = NS_PER_MS * MS_PER_S
BYTES_PER_MIB: int = 1024 * 1024


def ms_to_ns(ms: float) -> int:
    return round(ms * NS_PER_MS)


def ns_to_ms(ns: int) -> float:
    return ns / NS_PER_MS


def ms_to_s(ms: float) -> float:
    return ms / MS_PER_S


def s_to_ms(seconds: float) -> float:
    return seconds * MS_PER_S
