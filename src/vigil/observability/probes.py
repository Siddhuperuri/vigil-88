"""Host probes. Returns plain readings (observability is below domain in the layering);
pipeline/system_metrics.py composes them into the domain SystemMetric.

GPU probing is not implemented in P0; it arrives with the GPU path in P1. Until then GPU
fields are absent (None), never zero.
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil

from vigil.core.units import BYTES_PER_MIB

PERCENT_MAX = 100.0


@dataclass(frozen=True, slots=True)
class SystemReading:
    cpu_percent: float
    memory_used_mb: float
    process_rss_mb: float


class SystemProbe:
    def __init__(self) -> None:
        self._process = psutil.Process()
        # The first cpu_percent() call has no baseline and returns a meaningless 0.0: prime it.
        psutil.cpu_percent(interval=None)

    def read(self) -> SystemReading:
        cpu = min(max(psutil.cpu_percent(interval=None), 0.0), PERCENT_MAX)
        return SystemReading(
            cpu_percent=cpu,
            memory_used_mb=psutil.virtual_memory().used / BYTES_PER_MIB,
            process_rss_mb=self._process.memory_info().rss / BYTES_PER_MIB,
        )
