"""Restarting one worker: what it does, and everything it refuses to do."""

from __future__ import annotations

import threading
import time

import pytest

from vigil.core.clock import ManualClock
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.restartables import NOT_RESTARTABLE, register_restartables
from vigil.pipeline.supervision import Entry, WorkerSupervisor
from vigil.pipeline.workers import ThreadWorker


def worker(name: str, clock: ManualClock, target=None) -> ThreadWorker:  # type: ignore[no-untyped-def]
    def wait_for_stop(stop: threading.Event, beat) -> None:  # type: ignore[no-untyped-def]
        while not stop.wait(0.01):
            beat()

    return ThreadWorker(
        name,
        target or wait_for_stop,
        clock=clock,
        logger=get_logger("test.supervision"),
        metrics=MetricsRegistry(),
    )


class Rig:
    def __init__(self, **kw: float) -> None:
        self.clock = ManualClock()
        self.metrics = MetricsRegistry()
        self.supervisor = WorkerSupervisor(
            clock=self.clock,
            logger=get_logger("test.supervision"),
            metrics=self.metrics,
            **{"stop_join_s": 0.5, **kw},  # type: ignore[arg-type]
        )
        self.current: ThreadWorker = worker("w", self.clock)
        self.current.start()
        self.rebuilt = 0

        def rebuild() -> ThreadWorker:
            self.rebuilt += 1
            return worker("w", self.clock)

        self.supervisor.register(
            Entry("w", get=lambda: self.current, install=self._install, rebuild=rebuild)
        )

    def _install(self, fresh: ThreadWorker) -> None:
        self.current = fresh

    def close(self) -> None:
        self.current.request_stop()
        self.current.join(1)


@pytest.fixture
def rig() -> Rig:
    r = Rig()
    yield r  # type: ignore[misc]
    r.close()


def test_a_running_worker_is_stopped_replaced_and_started(rig: Rig) -> None:
    old = rig.current
    result = rig.supervisor.restart("w")
    assert result.ok and rig.current is not old
    assert not old.alive and rig.current.alive
    assert rig.metrics.counter("vigil_worker_restarts_total", worker="w").value == 1


def test_a_crashed_worker_is_replaced() -> None:
    clock = ManualClock()

    def boom(stop, beat) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("crashed")

    dead = worker("w", clock, boom)
    dead.start()
    assert dead.join(1) and dead.health().crashed
    holder = [dead]
    sup = WorkerSupervisor(clock=clock, logger=get_logger("t"), metrics=MetricsRegistry())
    sup.register(
        Entry(
            "w",
            get=lambda: holder[0],
            install=lambda fresh: holder.__setitem__(0, fresh),
            rebuild=lambda: worker("w", clock),
        )
    )
    assert sup.restart("w").ok and holder[0].alive and not holder[0].health().crashed
    holder[0].request_stop()
    holder[0].join(1)


def test_a_thread_that_will_not_stop_is_not_duplicated() -> None:
    clock = ManualClock()
    release = threading.Event()

    def stubborn(stop, beat) -> None:  # type: ignore[no-untyped-def]
        release.wait(10)  # ignores the stop request, as a thread stuck in a call would

    stuck = worker("w", clock, stubborn)
    stuck.start()
    built: list[int] = []
    sup = WorkerSupervisor(
        clock=clock, logger=get_logger("t"), metrics=MetricsRegistry(), stop_join_s=0.2
    )
    sup.register(
        Entry(
            "w",
            get=lambda: stuck,
            install=lambda f: None,
            rebuild=lambda: built.append(1) or worker("w", clock),
        )  # type: ignore[arg-type,return-value]
    )
    result = sup.restart("w")
    release.set()
    stuck.join(2)
    assert not result.ok and "still running" in result.detail and built == []


def test_an_unknown_worker_is_refused(rig: Rig) -> None:
    result = rig.supervisor.restart("nope")
    assert not result.ok and "no worker named" in result.detail


def test_a_worker_with_no_safe_rebuild_is_refused_with_its_reason() -> None:
    sup = WorkerSupervisor(clock=ManualClock(), logger=get_logger("t"), metrics=MetricsRegistry())
    sup.register(
        Entry("inference", get=lambda: None, install=lambda f: None, rebuild=None, refusal="no")
    )
    assert not sup.restartable("inference")
    assert sup.restart("inference").detail == "no"


def test_a_second_restart_straight_away_is_refused(rig: Rig) -> None:
    assert rig.supervisor.restart("w").ok
    again = rig.supervisor.restart("w")
    assert not again.ok and "moments ago" in again.detail
    assert rig.rebuilt == 1


def test_restarts_are_capped_within_a_window() -> None:
    r = Rig(cooldown_s=1.0, max_restarts=2, window_s=600)
    try:
        for _ in range(2):
            assert r.supervisor.restart("w").ok
            r.clock.advance_ms(2000)
        refused = r.supervisor.restart("w")
        assert not refused.ok and "restarted 2 times" in refused.detail
        r.clock.advance_ms(601_000)  # the window passes
        assert r.supervisor.restart("w").ok
    finally:
        r.close()


def test_a_rebuild_that_fails_is_reported_and_leaves_the_old_worker_installed() -> None:
    clock = ManualClock()
    original = worker("w", clock)
    original.start()
    holder = [original]

    def explode() -> ThreadWorker:
        raise RuntimeError("no source")

    sup = WorkerSupervisor(clock=clock, logger=get_logger("t"), metrics=MetricsRegistry())
    sup.register(
        Entry(
            "w", get=lambda: holder[0], install=lambda f: holder.__setitem__(0, f), rebuild=explode
        )
    )
    result = sup.restart("w")
    assert not result.ok and "could not be rebuilt: no source" in result.detail
    assert holder[0] is original
    original.join(0.05)


def test_names_are_listed_sorted(rig: Rig) -> None:
    assert rig.supervisor.names() == ("w",)


def test_registering_a_workers_list_marks_state_holders_as_not_restartable() -> None:
    clock = ManualClock()
    workers = [worker(n, clock) for n in ("inference", "analysis", "response", "sampler")]
    sup = WorkerSupervisor(clock=clock, logger=get_logger("t"), metrics=MetricsRegistry())
    register_restartables(sup, system_workers=workers, cameras={}, rebuild_capture=lambda rt: rt)  # type: ignore[arg-type,return-value]
    assert [sup.restartable(n) for n in ("inference", "analysis", "response", "sampler")] == [
        False,
        False,
        False,
        True,
    ]
    assert set(NOT_RESTARTABLE) == {"inference", "analysis", "response"}
    assert "restart the engine" in sup.restart("inference").detail


def test_a_system_worker_restarts_in_place_in_its_list() -> None:
    clock = ManualClock()
    workers = [worker("sampler", clock)]
    workers[0].start()
    sup = WorkerSupervisor(
        clock=clock, logger=get_logger("t"), metrics=MetricsRegistry(), stop_join_s=1
    )
    register_restartables(sup, system_workers=workers, cameras={}, rebuild_capture=lambda rt: rt)  # type: ignore[arg-type,return-value]
    first = workers[0]
    assert sup.restart("sampler").ok
    assert workers[0] is not first and workers[0].alive
    workers[0].request_stop()
    time.sleep(0.05)
