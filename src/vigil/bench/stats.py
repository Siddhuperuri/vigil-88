"""Benchmark statistics: pure functions over per-second samples.

A benchmark is only believable if its arithmetic is checkable, so everything here is a plain
function of plain data. Missing measurements (no NVML, a field the driver does not report) are
EXCLUDED from means and extrema; they are never counted as zero.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from vigil.observability.metrics import percentile


@dataclass(frozen=True, slots=True)
class SamplePoint:
    """One sampling interval (about a second). Counts are within the interval, not cumulative."""

    t_s: float  # end of the interval, seconds since the run started
    dt_s: float  # the interval's actual length
    received: int
    inferred: int
    skipped: int
    dropped: int
    infer_ms: tuple[float, ...]  # detector time (preprocess + run + postprocess) per frame
    stage_ms: Mapping[str, tuple[float, ...]]
    capture_to_result_ms: tuple[float, ...]  # camera frame captured -> detections available
    end_to_end_ms: tuple[float, ...]  # camera frame captured -> annotated JPEG ready
    cpu_percent: float
    process_cpu_percent: float  # a share of ONE core
    rss_mb: float
    gpu_util_percent: float | None
    gpu_used_mb: float | None
    gpu_temp_c: float | None
    gpu_sm_clock_mhz: float | None
    gpu_power_w: float | None
    gpu_throttle: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Dist:
    count: int
    mean: float | None
    p50: float | None
    p95: float | None
    p99: float | None
    maximum: float | None


@dataclass(frozen=True, slots=True)
class Range:
    mean: float | None
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True, slots=True)
class WindowSummary:
    seconds: float
    received: int
    inferred: int
    skipped: int
    dropped: int
    fps_mean: float | None  # frames inferred / seconds
    fps_median: float | None  # median of the per-second rates
    fps_low: float | None  # 10th percentile of the per-second rates
    infer_ms: Dist
    stage_ms: Mapping[str, Dist]
    capture_to_result_ms: Dist
    end_to_end_ms: Dist
    cpu_percent: Range
    process_cpu_percent: Range
    rss_mb: Range
    gpu_util_percent: Range
    gpu_used_mb: Range
    gpu_temp_c: Range
    gpu_sm_clock_mhz: Range
    gpu_power_w: Range
    throttled_seconds: int
    throttle_reasons: tuple[str, ...]


def dist(values: Iterable[float]) -> Dist:
    data = sorted(values)
    if not data:
        return Dist(0, None, None, None, None, None)
    return Dist(
        count=len(data),
        mean=statistics.fmean(data),
        p50=percentile(data, 50),
        p95=percentile(data, 95),
        p99=percentile(data, 99),
        maximum=data[-1],
    )


def value_range(values: Iterable[float | None]) -> Range:
    present = [v for v in values if v is not None]
    if not present:
        return Range(None, None, None)
    return Range(statistics.fmean(present), min(present), max(present))


def _flatten(parts: Iterable[Sequence[float]]) -> list[float]:
    return [v for part in parts for v in part]


def summarise(points: Sequence[SamplePoint]) -> WindowSummary:
    seconds = sum(p.dt_s for p in points)
    inferred = sum(p.inferred for p in points)
    rates = sorted(p.inferred / p.dt_s for p in points if p.dt_s > 0)
    stages = sorted({s for p in points for s in p.stage_ms})
    reasons = sorted({r for p in points for r in p.gpu_throttle})
    return WindowSummary(
        seconds=seconds,
        received=sum(p.received for p in points),
        inferred=inferred,
        skipped=sum(p.skipped for p in points),
        dropped=sum(p.dropped for p in points),
        fps_mean=inferred / seconds if seconds > 0 else None,
        fps_median=statistics.median(rates) if rates else None,
        fps_low=percentile(rates, 10) if rates else None,
        infer_ms=dist(_flatten(p.infer_ms for p in points)),
        stage_ms={s: dist(_flatten(p.stage_ms.get(s, ()) for p in points)) for s in stages},
        capture_to_result_ms=dist(_flatten(p.capture_to_result_ms for p in points)),
        end_to_end_ms=dist(_flatten(p.end_to_end_ms for p in points)),
        cpu_percent=value_range(p.cpu_percent for p in points),
        process_cpu_percent=value_range(p.process_cpu_percent for p in points),
        rss_mb=value_range(p.rss_mb for p in points),
        gpu_util_percent=value_range(p.gpu_util_percent for p in points),
        gpu_used_mb=value_range(p.gpu_used_mb for p in points),
        gpu_temp_c=value_range(p.gpu_temp_c for p in points),
        gpu_sm_clock_mhz=value_range(p.gpu_sm_clock_mhz for p in points),
        gpu_power_w=value_range(p.gpu_power_w for p in points),
        throttled_seconds=sum(1 for p in points if p.gpu_throttle),
        throttle_reasons=tuple(reasons),
    )


def window(points: Sequence[SamplePoint], start_s: float, end_s: float) -> list[SamplePoint]:
    """Samples whose interval ENDS inside (start_s, end_s]."""
    return [p for p in points if start_s < p.t_s <= end_s]


def first_and_last(
    points: Sequence[SamplePoint], duration_s: float, window_s: float
) -> tuple[WindowSummary, WindowSummary]:
    """The first and last `window_s` seconds: how performance changes under sustained load."""
    first = summarise(window(points, 0.0, window_s))
    last = summarise(window(points, max(duration_s - window_s, 0.0), duration_s))
    return first, last


def change_percent(first: float | None, last: float | None) -> float | None:
    """How much `last` differs from `first`, in percent of `first`."""
    if first is None or last is None or first == 0:
        return None
    return (last - first) / first * 100.0
