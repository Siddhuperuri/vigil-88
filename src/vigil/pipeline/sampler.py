"""Periodic system sampling into SystemMetric records and gauges."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable

from vigil.core.clock import Clock
from vigil.domain.metric import SystemMetric
from vigil.observability.metrics import MetricsRegistry
from vigil.observability.probes import SystemProbe


class MetricsSampler:
    def __init__(
        self,
        *,
        probe: SystemProbe,
        metrics: MetricsRegistry,
        clock: Clock,
        interval_ms: int,
        history_len: int,
    ) -> None:
        self._probe = probe
        self._metrics = metrics
        self._clock = clock
        self._interval_s = interval_ms / 1000.0
        self._lock = threading.Lock()
        self._history: deque[SystemMetric] = deque(maxlen=history_len)

    def sample(self) -> SystemMetric:
        reading = self._probe.read()
        gpu = reading.gpu
        m = self._metrics
        latency = m.histogram("vigil_inference_latency_ms").summary()
        fps_gauge = m.gauge("vigil_pipeline_fps").value
        depths = {
            ",".join(f"{k}={v}" for k, v in labels): int(value)
            for labels, value in m.gauges_named("vigil_queue_depth").items()
        }
        metric = SystemMetric(
            t_wall_utc=self._clock.wall_utc(),
            cpu_percent=reading.cpu_percent,
            memory_used_mb=reading.memory_used_mb,
            process_rss_mb=reading.process_rss_mb,
            process_cpu_percent=reading.process_cpu_percent,
            # None whenever NVML is absent or a field is unsupported: never reported as zero.
            gpu_utilization_percent=gpu.utilization_percent if gpu else None,
            gpu_memory_used_mb=gpu.used_mb if gpu else None,
            gpu_temperature_c=gpu.temperature_c if gpu else None,
            gpu_sm_clock_mhz=gpu.sm_clock_mhz if gpu else None,
            gpu_power_w=gpu.power_w if gpu else None,
            gpu_throttle_reasons=gpu.throttle_reasons if gpu else (),
            pipeline_fps=fps_gauge,
            inference_latency_p50_ms=latency.p50,
            inference_latency_p95_ms=latency.p95,
            queue_depths=depths,
            frames_dropped_total=int(m.counter_total("vigil_frames_dropped_total")),
        )
        m.gauge("vigil_cpu_percent").set(metric.cpu_percent)
        m.gauge("vigil_process_rss_mb").set(metric.process_rss_mb)
        with self._lock:
            self._history.append(metric)
        return metric

    def latest(self) -> SystemMetric | None:
        with self._lock:
            return self._history[-1] if self._history else None

    def history(self) -> tuple[SystemMetric, ...]:
        with self._lock:
            return tuple(self._history)

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        while not stop.is_set():
            beat()
            self.sample()
            stop.wait(self._interval_s)
