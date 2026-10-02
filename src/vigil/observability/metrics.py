"""In-process metrics registry (01 §7.3): counters, gauges and bounded histograms.

A write costs one lock acquisition and one store; aggregation (percentiles) happens on read.
Histograms keep a bounded window of recent samples, so memory is constant over a long shift.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

LabelKey = tuple[tuple[str, str], ...]
SeriesKey = tuple[str, LabelKey]


def _label_key(labels: Mapping[str, str]) -> LabelKey:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


@dataclass(frozen=True, slots=True)
class HistogramSummary:
    count: int  # all-time observations
    window: int  # samples currently held
    mean: float | None
    minimum: float | None
    maximum: float | None
    p50: float | None
    p95: float | None
    p99: float | None


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile of an already-sorted, non-empty list."""
    if not sorted_values:
        raise ValueError("percentile of an empty sample")
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


class Counter:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._value = 0.0

    def inc(self, amount: float = 1.0) -> None:
        if amount < 0:
            raise ValueError("a counter cannot decrease")
        with self._lock:
            self._value += amount

    @property
    def value(self) -> float:
        with self._lock:
            return self._value


class Gauge:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._value: float | None = None

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value

    @property
    def value(self) -> float | None:
        """None until first set: 'never measured' is not the same as zero."""
        with self._lock:
            return self._value


class Histogram:
    def __init__(self, lock: threading.Lock, max_samples: int) -> None:
        self._lock = lock
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._count = 0
        self._total = 0.0

    def observe(self, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError(f"histogram observation must be finite, got {value!r}")
        with self._lock:
            self._samples.append(value)
            self._count += 1
            self._total += value

    def summary(self) -> HistogramSummary:
        with self._lock:
            data = sorted(self._samples)
            count = self._count
        if not data:
            return HistogramSummary(count, 0, None, None, None, None, None, None)
        return HistogramSummary(
            count=count,
            window=len(data),
            mean=sum(data) / len(data),
            minimum=data[0],
            maximum=data[-1],
            p50=percentile(data, 50),
            p95=percentile(data, 95),
            p99=percentile(data, 99),
        )

    def samples(self) -> tuple[float, ...]:
        with self._lock:
            return tuple(self._samples)


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    counters: Mapping[SeriesKey, float]
    gauges: Mapping[SeriesKey, float | None]
    histograms: Mapping[SeriesKey, HistogramSummary]


class MetricsRegistry:
    def __init__(self, *, histogram_samples: int = 2048) -> None:
        self._lock = threading.Lock()
        self._histogram_samples = histogram_samples
        self._counters: dict[SeriesKey, Counter] = {}
        self._gauges: dict[SeriesKey, Gauge] = {}
        self._histograms: dict[SeriesKey, Histogram] = {}

    def counter(self, name: str, **labels: str) -> Counter:
        key = (name, _label_key(labels))
        with self._lock:
            if key not in self._counters:
                self._counters[key] = Counter(self._lock)
            return self._counters[key]

    def gauge(self, name: str, **labels: str) -> Gauge:
        key = (name, _label_key(labels))
        with self._lock:
            if key not in self._gauges:
                self._gauges[key] = Gauge(self._lock)
            return self._gauges[key]

    def histogram(self, name: str, **labels: str) -> Histogram:
        key = (name, _label_key(labels))
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = Histogram(self._lock, self._histogram_samples)
            return self._histograms[key]

    def counter_total(self, name: str) -> float:
        """Sum of one counter across all of its label sets."""
        with self._lock:
            counters = [c for (n, _), c in self._counters.items() if n == name]
        return sum(c.value for c in counters)

    def gauges_named(self, name: str) -> dict[LabelKey, float]:
        with self._lock:
            gauges = [(labels, g) for (n, labels), g in self._gauges.items() if n == name]
        return {labels: v for labels, g in gauges if (v := g.value) is not None}

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = dict(self._histograms)
        return MetricsSnapshot(
            counters={k: c.value for k, c in counters.items()},
            gauges={k: g.value for k, g in gauges.items()},
            histograms={k: h.summary() for k, h in histograms.items()},
        )
