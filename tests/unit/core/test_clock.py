from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from vigil.core.clock import ManualClock, ReplayClock, SystemClock


def test_manual_clock_only_moves_when_told() -> None:
    c = ManualClock()
    assert c.monotonic_ns() == c.monotonic_ns() == 0
    c.advance_ms(250)
    assert c.monotonic_ns() == 250_000_000


def test_manual_wall_time_tracks_monotonic() -> None:
    start = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    c = ManualClock(wall_start=start)
    c.advance_ms(1500)
    assert c.wall_utc() == start + timedelta(milliseconds=1500)


def test_manual_clock_cannot_go_backwards() -> None:
    with pytest.raises(ValueError, match="backwards"):
        ManualClock().advance_ns(-1)


def test_manual_clock_requires_aware_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(wall_start=datetime(2026, 1, 1))  # noqa: DTZ001


def test_manual_clock_normalises_to_utc() -> None:
    plus5 = timezone(timedelta(hours=5))
    c = ManualClock(wall_start=datetime(2026, 1, 1, 5, 0, tzinfo=plus5))
    assert c.wall_utc() == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert c.wall_utc().utcoffset() == timedelta(0)


def test_replay_clock_is_driven_by_pts() -> None:
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    c = ReplayClock(wall_anchor=anchor)
    c.seek_pts_ms(40.0)
    c.seek_pts_ms(80.0)
    assert c.monotonic_ns() == 80_000_000
    assert c.wall_utc() == anchor + timedelta(milliseconds=80)


def test_replay_clock_is_independent_of_how_fast_it_is_driven() -> None:
    """The point of ReplayClock: 30 fps replayed at any speed yields identical spacing."""
    c = ReplayClock(wall_anchor=datetime(2026, 1, 1, tzinfo=UTC))
    stamps = []
    for i in range(5):
        c.seek_pts_ms(i * 1000 / 30)
        stamps.append(c.monotonic_ns())
    gaps = {b - a for a, b in zip(stamps, stamps[1:], strict=False)}
    assert max(gaps) - min(gaps) <= 1  # equal up to nanosecond rounding


def test_replay_clock_rejects_backwards_pts() -> None:
    c = ReplayClock(wall_anchor=datetime(2026, 1, 1, tzinfo=UTC))
    c.seek_pts_ms(100.0)
    with pytest.raises(ValueError, match="backwards"):
        c.seek_pts_ms(50.0)


def test_system_clock_is_monotonic_and_utc() -> None:
    c = SystemClock()
    a, b = c.monotonic_ns(), c.monotonic_ns()
    assert b >= a
    assert c.wall_utc().utcoffset() == timedelta(0)
