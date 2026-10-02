from __future__ import annotations

import threading

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigil.observability.metrics import MetricsRegistry, percentile
from vigil.observability.probes import SystemProbe


def test_counter_counts_and_cannot_decrease() -> None:
    c = MetricsRegistry().counter("x")
    c.inc()
    c.inc(2.5)
    assert c.value == 3.5
    with pytest.raises(ValueError, match="cannot decrease"):
        c.inc(-1)


def test_gauge_is_none_until_it_has_been_measured() -> None:
    g = MetricsRegistry().gauge("g")
    assert g.value is None  # never measured is not the same as zero
    g.set(0.0)
    assert g.value == 0.0


def test_same_name_and_labels_share_one_series() -> None:
    r = MetricsRegistry()
    r.counter("f", camera="a").inc()
    r.counter("f", camera="a").inc()
    r.counter("f", camera="b").inc()
    assert r.counter("f", camera="a").value == 2
    assert r.counter_total("f") == 3


def test_label_order_does_not_matter() -> None:
    r = MetricsRegistry()
    r.counter("f", a="1", b="2").inc()
    assert r.counter("f", b="2", a="1").value == 1


def test_percentile_is_nearest_rank() -> None:
    data = list(map(float, range(1, 101)))
    assert percentile(data, 50) == 50.0
    assert percentile(data, 95) == 95.0
    assert percentile(data, 99) == 99.0
    assert percentile([7.0], 99) == 7.0
    with pytest.raises(ValueError):
        percentile([], 50)


def test_histogram_summary() -> None:
    h = MetricsRegistry().histogram("lat")
    assert h.summary().p50 is None and h.summary().count == 0
    for v in range(1, 101):
        h.observe(float(v))
    s = h.summary()
    assert (s.count, s.window, s.minimum, s.maximum) == (100, 100, 1.0, 100.0)
    assert s.p50 == 50.0 and s.p95 == 95.0 and s.mean == pytest.approx(50.5)


def test_histogram_memory_is_bounded_but_count_is_all_time() -> None:
    h = MetricsRegistry(histogram_samples=10).histogram("lat")
    for v in range(100):
        h.observe(float(v))
    s = h.summary()
    assert s.count == 100 and s.window == 10 and s.minimum == 90.0


def test_histogram_rejects_non_finite_values() -> None:
    h = MetricsRegistry().histogram("lat")
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            h.observe(bad)


@given(st.lists(st.floats(0, 1e6), min_size=1, max_size=200))
def test_percentiles_are_ordered_and_within_range(values: list[float]) -> None:
    h = MetricsRegistry().histogram("x")
    for v in values:
        h.observe(v)
    s = h.summary()
    assert s.minimum is not None and s.p50 is not None and s.p95 is not None
    assert s.p99 is not None and s.maximum is not None
    assert s.minimum <= s.p50 <= s.p95 <= s.p99 <= s.maximum


def test_counters_are_thread_safe() -> None:
    r = MetricsRegistry()

    def work() -> None:
        for _ in range(2000):
            r.counter("n").inc()

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert r.counter("n").value == 16_000


def test_gauges_named_and_snapshot() -> None:
    r = MetricsRegistry()
    r.gauge("depth", queue="a").set(3)
    r.gauge("depth", queue="b").set(5)
    r.gauge("depth", queue="never-set")
    r.counter("c").inc()
    r.histogram("h").observe(1.0)
    assert sorted(r.gauges_named("depth").values()) == [3.0, 5.0]
    snap = r.snapshot()
    assert snap.counters[("c", ())] == 1
    assert snap.histograms[("h", ())].count == 1
    assert snap.gauges[("depth", (("queue", "never-set"),))] is None


def test_system_probe_reports_plausible_values() -> None:
    reading = SystemProbe().read()
    assert 0.0 <= reading.cpu_percent <= 100.0
    assert reading.memory_used_mb > 0 and reading.process_rss_mb > 0
    assert reading.process_rss_mb < reading.memory_used_mb * 10
