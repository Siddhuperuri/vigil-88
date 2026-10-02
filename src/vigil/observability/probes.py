"""Host and GPU probes. Returns plain readings (observability is below domain in the
layering); pipeline/sampler.py composes them into the domain SystemMetric.

GPU data comes from NVML (`nvidia-ml-py`, in the onnx-gpu extra). Without it, or without an
NVIDIA driver, every GPU field is None: unmeasured is never reported as zero.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import psutil

from vigil.core.units import BYTES_PER_MIB

PERCENT_MAX = 100.0
_MW_PER_W = 1000.0

# NVML throttle-reason bits worth naming (nvmlClocksThrottleReason*).
_THROTTLE_BITS: tuple[tuple[int, str], ...] = (
    (0x0000000000000001, "gpu_idle"),
    (0x0000000000000002, "app_clocks_setting"),
    (0x0000000000000004, "sw_power_cap"),
    (0x0000000000000008, "hw_slowdown"),
    (0x0000000000000020, "sw_thermal_slowdown"),
    (0x0000000000000040, "hw_thermal_slowdown"),
    (0x0000000000000080, "hw_power_brake_slowdown"),
)
# Reasons that mean the GPU is being held back, as opposed to merely idle.
_LIMITING = frozenset(
    {
        "sw_power_cap",
        "hw_slowdown",
        "sw_thermal_slowdown",
        "hw_thermal_slowdown",
        "hw_power_brake_slowdown",
    }
)


@dataclass(frozen=True, slots=True)
class GpuReading:
    index: int
    name: str
    total_mb: float
    used_mb: float | None
    utilization_percent: float | None
    temperature_c: float | None
    sm_clock_mhz: float | None
    max_sm_clock_mhz: float | None
    power_w: float | None
    power_limit_w: float | None
    throttle_reasons: tuple[str, ...]  # only the limiting ones; empty means not throttled

    @property
    def throttled(self) -> bool:
        return bool(self.throttle_reasons)


@dataclass(frozen=True, slots=True)
class SystemReading:
    cpu_percent: float
    memory_used_mb: float
    process_rss_mb: float
    process_cpu_percent: float
    gpu: GpuReading | None


class NvmlProbe:
    """Thin, failure-tolerant wrapper over NVML. Never raises from `read`."""

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self.reason: str | None = None
        self._nvml: ModuleType | None = None
        self._handle: object = None
        self._name = ""
        self.driver_version: str | None = None
        self._total_mb = 0.0
        try:
            import pynvml  # nvidia-ml-py

            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            raw_name = pynvml.nvmlDeviceGetName(self._handle)
            self._name = raw_name.decode() if isinstance(raw_name, bytes) else str(raw_name)
            self._total_mb = pynvml.nvmlDeviceGetMemoryInfo(self._handle).total / BYTES_PER_MIB
            driver = pynvml.nvmlSystemGetDriverVersion()
            self.driver_version = driver.decode() if isinstance(driver, bytes) else str(driver)
            self._nvml = pynvml
        except ImportError:
            self.reason = "nvidia-ml-py is not installed (extra: onnx-gpu)"
        except Exception as exc:  # noqa: BLE001 - NVML raises its own error types
            self.reason = f"NVML unavailable: {type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._nvml is not None

    def _try(self, fn_name: str, *args: object) -> Any:
        """One NVML query. A field the driver does not support yields None, not an error."""
        nvml = self._nvml
        if nvml is None:
            return None
        try:
            return getattr(nvml, fn_name)(self._handle, *args)
        except Exception:  # noqa: BLE001 - a single unsupported field must not hide the rest
            return None

    def read(self) -> GpuReading | None:
        if self._nvml is None:
            return None
        nvml = self._nvml
        mem = self._try("nvmlDeviceGetMemoryInfo")
        util = self._try("nvmlDeviceGetUtilizationRates")
        temp = self._try("nvmlDeviceGetTemperature", nvml.NVML_TEMPERATURE_GPU)
        sm = self._try("nvmlDeviceGetClockInfo", nvml.NVML_CLOCK_SM)
        sm_max = self._try("nvmlDeviceGetMaxClockInfo", nvml.NVML_CLOCK_SM)
        power = self._try("nvmlDeviceGetPowerUsage")
        limit = self._try("nvmlDeviceGetEnforcedPowerLimit")
        bits = self._try("nvmlDeviceGetCurrentClocksThrottleReasons")
        reasons = (
            tuple(n for b, n in _THROTTLE_BITS if int(bits) & b and n in _LIMITING)
            if bits is not None
            else ()
        )
        return GpuReading(
            index=self.index,
            name=self._name,
            total_mb=self._total_mb,
            used_mb=mem.used / BYTES_PER_MIB if mem is not None else None,
            utilization_percent=float(util.gpu) if util is not None else None,
            temperature_c=float(temp) if temp is not None else None,
            sm_clock_mhz=float(sm) if sm is not None else None,
            max_sm_clock_mhz=float(sm_max) if sm_max is not None else None,
            power_w=power / _MW_PER_W if power is not None else None,
            power_limit_w=limit / _MW_PER_W if limit is not None else None,
            throttle_reasons=reasons,
        )

    def process_vram_mb(self, pid: int | None = None) -> float | None:
        """This process's GPU memory, if the driver reports it (WDDM often does not)."""
        nvml = self._nvml
        if nvml is None:
            return None
        target = os.getpid() if pid is None else pid
        for getter in (
            "nvmlDeviceGetComputeRunningProcesses",
            "nvmlDeviceGetGraphicsRunningProcesses",
        ):
            procs = self._try(getter) or []
            for p in procs:
                used = getattr(p, "usedGpuMemory", None)
                if p.pid == target and used is not None and used < 2**62:
                    return float(used) / BYTES_PER_MIB
        return None

    def shutdown(self) -> None:
        nvml, self._nvml = self._nvml, None
        if nvml is not None:
            # An explicit handler, not contextlib.suppress: the repo gate sees and approves it.
            try:  # noqa: SIM105
                nvml.nvmlShutdown()
            except Exception:  # noqa: BLE001, S110  # gate: allow best-effort NVML teardown at exit
                pass


class SystemProbe:
    def __init__(self, gpu: NvmlProbe | None = None) -> None:
        self._process = psutil.Process()
        self._gpu = gpu
        # The first cpu_percent() call has no baseline and returns a meaningless 0.0: prime it.
        psutil.cpu_percent(interval=None)
        self._process.cpu_percent(interval=None)

    def read(self) -> SystemReading:
        cpu = min(max(psutil.cpu_percent(interval=None), 0.0), PERCENT_MAX)
        # Process CPU is a share of ONE core (can exceed 100 on a multi-threaded process).
        return SystemReading(
            cpu_percent=cpu,
            memory_used_mb=psutil.virtual_memory().used / BYTES_PER_MIB,
            process_rss_mb=self._process.memory_info().rss / BYTES_PER_MIB,
            process_cpu_percent=max(self._process.cpu_percent(interval=None), 0.0),
            gpu=self._gpu.read() if self._gpu is not None else None,
        )
