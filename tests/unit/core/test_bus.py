from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from vigil.core.bus import EventBus


@dataclass
class Ping:
    n: int


@dataclass
class SubPing(Ping):
    pass


@dataclass
class Other:
    pass


def test_delivers_to_matching_subscribers_only() -> None:
    bus, got = EventBus(), []
    bus.subscribe(Ping, got.append)
    assert bus.publish(Ping(1)) == 1
    assert bus.publish(Other()) == 0
    assert got == [Ping(1)]


def test_subclass_events_reach_base_class_subscribers() -> None:
    bus, got = EventBus(), []
    bus.subscribe(Ping, got.append)
    bus.publish(SubPing(2))
    assert got == [SubPing(2)]


def test_cancelled_subscription_stops_receiving() -> None:
    bus, got = EventBus(), []
    sub = bus.subscribe(Ping, got.append)
    sub.cancel()
    sub.cancel()  # idempotent
    bus.publish(Ping(1))
    assert got == []


def test_failing_subscriber_is_isolated_and_reported() -> None:
    errors: list[tuple[object, BaseException]] = []
    bus = EventBus(on_handler_error=lambda e, x: errors.append((e, x)))
    seen = []

    def boom(_: Ping) -> None:
        raise RuntimeError("nope")

    bus.subscribe(Ping, boom)
    bus.subscribe(Ping, seen.append)
    assert bus.publish(Ping(3)) == 2
    assert seen == [Ping(3)]  # the second subscriber still ran
    assert len(errors) == 1 and isinstance(errors[0][1], RuntimeError)


def test_default_error_hook_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()

    def boom(_: Ping) -> None:
        raise RuntimeError("nope")

    bus.subscribe(Ping, boom)
    with caplog.at_level(logging.WARNING, logger="vigil.core.bus"):
        bus.publish(Ping(1))
    assert any("subscriber failed" in r.message for r in caplog.records)


def test_subscribing_during_dispatch_is_safe() -> None:
    bus, late = EventBus(), []

    def add_late(_: Ping) -> None:
        bus.subscribe(Ping, late.append)

    bus.subscribe(Ping, add_late)
    bus.publish(Ping(1))
    assert late == []  # not delivered the event that caused the subscription
    bus.publish(Ping(2))
    assert late == [Ping(2)]
