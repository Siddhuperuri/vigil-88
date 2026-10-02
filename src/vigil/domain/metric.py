"""System metrics (05 §4). Every unmeasurable field is None, never 0 (05 §1 rule 5)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from vigil.domain._validate import (
    frozen_mapping,
    require_non_empty,
    require_non_negative,
    require_utc,
)

PERCENT_MAX = 100.0


@dataclass(frozen=True, slots=True)
class StageLatency:
    stage: str
    camera_id: str | None
    p50_ms: float
    p95_ms: float
    p99_ms: float
    sample_count: int

    def __post_init__(self) -> None:
        require_non_empty("stage", self.stage)
        for name in ("p50_ms", "p95_ms", "p99_ms"):
            require_non_negative(name, getattr(self, name))
        require_non_negative("sample_count", self.sample_count)


@dataclass(frozen=True, slots=True)
class SystemMetric:
    t_wall_utc: datetime
    cpu_percent: float
    memory_used_mb: float
    process_rss_mb: float
    process_cpu_percent: float  # share of ONE core; a multi-threaded process can exceed 100
    gpu_utilization_percent: float | None
    gpu_memory_used_mb: float | None
    gpu_temperature_c: float | None
    gpu_sm_clock_mhz: float | None
    gpu_power_w: float | None
    gpu_throttle_reasons: tuple[str, ...]  # limiting reasons only; empty means not throttled
    pipeline_fps: float | None
    inference_latency_p50_ms: float | None
    inference_latency_p95_ms: float | None
    queue_depths: Mapping[str, int]
    frames_dropped_total: int

    def __post_init__(self) -> None:
        require_utc("t_wall_utc", self.t_wall_utc)
        if not 0.0 <= self.cpu_percent <= PERCENT_MAX:
            raise ValueError("cpu_percent must be in [0, 100]")
        require_non_negative("memory_used_mb", self.memory_used_mb)
        require_non_negative("process_rss_mb", self.process_rss_mb)
        require_non_negative("process_cpu_percent", self.process_cpu_percent)
        require_non_negative("frames_dropped_total", self.frames_dropped_total)
        object.__setattr__(self, "gpu_throttle_reasons", tuple(self.gpu_throttle_reasons))
        if self.gpu_utilization_percent is not None and not (
            0.0 <= self.gpu_utilization_percent <= PERCENT_MAX
        ):
            raise ValueError("gpu_utilization_percent must be in [0, 100]")
        object.__setattr__(self, "queue_depths", frozen_mapping(self.queue_depths))
