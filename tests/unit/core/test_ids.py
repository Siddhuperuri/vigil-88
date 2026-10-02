from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vigil.core.clock import ManualClock
from vigil.core.ids import (
    UlidFactory,
    display_ref,
    is_valid_ulid,
    ulid_timestamp_ms,
    validate_camera_id,
)
from vigil.core.rng import seeded_rng


@pytest.mark.parametrize("good", ["gate-north", "cam_1", "a1", "0cam", "x" * 39])
def test_valid_camera_ids(good: str) -> None:
    assert validate_camera_id(good) == good


@pytest.mark.parametrize(
    "bad", ["", "a", "Gate", "-lead", "_lead", "has space", "dot.dot", "x" * 40, "ünï", "a/b"]
)
def test_invalid_camera_ids(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid camera_id"):
        validate_camera_id(bad)


def factory(clock: ManualClock, seed: int = 1) -> UlidFactory:
    return UlidFactory(clock, seeded_rng(seed, "ulid"))


def test_ulid_shape_and_timestamp_roundtrip(clock: ManualClock) -> None:
    u = factory(clock).new()
    assert len(u) == 26 and is_valid_ulid(u)
    assert ulid_timestamp_ms(u) == int(clock.wall_utc().timestamp() * 1000)


def test_ulids_are_monotonic_within_one_millisecond(clock: ManualClock) -> None:
    f = factory(clock)
    ids = [f.new() for _ in range(500)]  # clock never advances
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_ulids_sort_by_creation_time_across_milliseconds(clock: ManualClock) -> None:
    f = factory(clock)
    ids = []
    for _ in range(50):
        ids.append(f.new())
        clock.advance_ms(3)
    assert ids == sorted(ids)


def test_ulids_are_reproducible_for_a_seed() -> None:
    a = [factory(ManualClock(), seed=7).new() for _ in range(3)]
    b = [factory(ManualClock(), seed=7).new() for _ in range(3)]
    assert a == b
    assert a != [factory(ManualClock(), seed=8).new() for _ in range(3)]


@pytest.mark.parametrize("bad", ["", "short", "I" * 26, "8" + "0" * 25, "0" * 25 + "U"])
def test_is_valid_ulid_rejects(bad: str) -> None:
    assert not is_valid_ulid(bad)


def test_timestamp_of_invalid_ulid_raises() -> None:
    with pytest.raises(ValueError, match="not a ULID"):
        ulid_timestamp_ms("nope")


def test_display_ref_format(clock: ManualClock) -> None:
    u = factory(clock).new()
    ref = display_ref(u, datetime(2026, 10, 1, 2, 41, tzinfo=UTC))
    assert ref == f"INC-20261001-{u[-6:]}"


def test_display_ref_rejects_non_ulid() -> None:
    with pytest.raises(ValueError, match="not a ULID"):
        display_ref("nope", datetime(2026, 1, 1, tzinfo=UTC))
