"""Run a sustained benchmark against the REAL application.

The benchmark builds the same Application, capture workers, scheduler, detector and stream
encoder as `vigil run`, then samples it once a second. There is no benchmark-only fast path, so
the numbers describe the production code. Nothing is estimated: every figure is read from the
metrics registry, from NVML, or from psutil, and a field that could not be measured is reported
as unmeasured rather than as zero.
"""

from __future__ import annotations

import importlib.metadata
import platform
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from vigil.bench.spec import BenchSpec
from vigil.bench.stats import SamplePoint, WindowSummary, first_and_last, summarise
from vigil.config.loader import load_settings
from vigil.core.clock import Clock, SystemClock
from vigil.core.errors import ConfigError, VigilError
from vigil.core.protocols.transport import StreamKey
from vigil.core.units import NS_PER_S
from vigil.observability.logging import configure_logging, get_logger, shutdown_logging
from vigil.observability.metrics import Histogram
from vigil.observability.probes import NvmlProbe, SystemProbe
from vigil.pipeline.app import Application
from vigil.pipeline.health import HealthLevel
from vigil.version import __version__

if TYPE_CHECKING:
    from vigil.pipeline.streaming import StreamHub

TICK_S = 1.0
ABORT_CHECK_S = 5.0
DEFAULT_OVERLAY = "boxes+labels+info"
STAGES = ("preprocess", "infer", "postprocess")
_NVIDIA_WHEELS = ("nvidia-cudnn-cu12", "nvidia-cublas-cu12", "nvidia-cuda-runtime-cu12")


class BenchError(VigilError):
    """The benchmark could not run, or the system under test failed while it ran."""


@dataclass(frozen=True, slots=True)
class BenchResult:
    spec: BenchSpec
    environment: Mapping[str, object]
    started_utc: str
    duration_s: float
    load_s: float
    model: Mapping[str, object]
    vram_baseline_mb: float | None
    points: tuple[SamplePoint, ...]
    whole: WindowSummary
    first: WindowSummary
    last: WindowSummary
    stream_frames_delivered: int
    health_issues: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def sustained(self) -> bool:
        return self.spec.sustained and self.duration_s >= self.spec.duration_s * 0.98


class _Cursor:
    """The samples a histogram has gained since the last call."""

    def __init__(self, histogram: Histogram) -> None:
        self._h = histogram
        self._seen = 0

    def take(self) -> tuple[float, ...]:
        count = self._h.summary().count
        new, self._seen = count - self._seen, count
        if new <= 0:
            return ()
        samples = self._h.samples()
        return samples[-new:] if new < len(samples) else samples


def _overrides(spec: BenchSpec) -> dict[str, object]:
    cameras: list[dict[str, object]] = []
    for i in range(spec.cameras):
        source: dict[str, object] = {
            "width_px": spec.width_px,
            "height_px": spec.height_px,
            "fps": spec.source_fps,
        }
        if spec.source == "webcam":
            source |= {"kind": "webcam", "device_index": 0}
            cid = "bench-webcam"
        else:
            source |= {"kind": "synthetic", "realtime": True, "seed": i}
            cid = f"bench-{i}"
        cameras.append({"camera_id": cid, "target_inference_fps": spec.infer_fps, "source": source})
    total = spec.infer_fps * spec.cameras
    return {
        "cameras": cameras,
        "vision.backend": "onnxruntime",
        "vision.weights": spec.detector,
        "vision.device": spec.device,
        # a benchmark must never silently measure the wrong device
        "vision.allow_cpu_fallback": False,
        "pipeline.global_inference_fps": max(total, 1.0) * 2,
        "pipeline.min_inference_fps": 1.0,
        "logging.level": "WARNING",
        "logging.file_enabled": False,
    }


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _on_ac_power() -> bool | None:
    """Plugged in? On a laptop this changes GPU power limits and clocks, so it is part of every
    result. None means the machine reports no battery (a desktop) or it could not be read."""
    battery = psutil.sensors_battery()
    return None if battery is None else bool(battery.power_plugged)


def collect_environment(
    app: Application, nvml: NvmlProbe | None, extra: Mapping[str, object]
) -> dict[str, object]:
    vm = psutil.virtual_memory()
    gpu = nvml.read() if nvml is not None else None
    env: dict[str, object] = {
        "vigil": __version__,
        "config_hash": app.settings.config_hash(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "cpu_logical": psutil.cpu_count(),
        "cpu_physical": psutil.cpu_count(logical=False),
        "ram_gb": round(vm.total / 2**30, 1),
        "ac_power": _on_ac_power(),
        "onnxruntime": _version("onnxruntime-gpu") or _version("onnxruntime"),
        "onnxruntime_build": "gpu" if _version("onnxruntime-gpu") else "cpu",
        "cuda_wheels": {w: _version(w) for w in _NVIDIA_WHEELS if _version(w)},
        "numpy": _version("numpy"),
        "opencv": _version("opencv-python-headless"),
        "gpu": None
        if gpu is None
        else {
            "name": gpu.name,
            "vram_total_mb": round(gpu.total_mb),
            "power_limit_w": gpu.power_limit_w,
            "max_sm_clock_mhz": gpu.max_sm_clock_mhz,
            "driver": nvml.driver_version if nvml is not None else None,
        },
    }
    env.update(extra)
    return env


def _drain(hub: StreamHub, key: StreamKey, stop: threading.Event, counter: list[int]) -> None:
    """An in-process stand-in for a browser tab: subscribe, then take every frame."""
    with hub.subscribe(key):
        last = 0
        while not stop.is_set():
            published = hub.wait_next(key, last, 0.5)
            if published is not None:
                last = published.seq
                counter[0] += 1


def run_benchmark(
    spec: BenchSpec,
    *,
    config_dir: Path | None = None,
    clock: Clock | None = None,
    extra_environment: Mapping[str, object] | None = None,
    on_sample: Callable[[SamplePoint], None] | None = None,
) -> BenchResult:
    spec.validate()
    clock = clock or SystemClock()
    try:
        loaded = load_settings(config_dir=config_dir, overrides=_overrides(spec))
    except ConfigError as exc:
        raise BenchError(f"benchmark configuration is invalid: {exc}") from exc

    configure_logging(loaded.settings.logging, log_file=None)  # WARNING and above only
    baseline_probe = NvmlProbe()
    baseline = baseline_probe.read()
    vram_baseline = baseline.used_mb if baseline is not None else None
    baseline_probe.shutdown()

    hub = None
    if spec.stream:
        from vigil.pipeline.streaming import StreamHub

        hub = StreamHub()
    app = Application(loaded.settings, loaded.paths, stream_hub=hub, logger=get_logger("bench"))

    load_started = clock.monotonic_ns()
    try:
        app.start()  # loads the model, warms it up and starts every worker
    except VigilError as exc:
        raise BenchError(f"the application failed to start: {exc}") from exc
    load_s = (clock.monotonic_ns() - load_started) / NS_PER_S

    stop = threading.Event()
    delivered: list[list[int]] = []
    threads: list[threading.Thread] = []
    camera_ids = [c.camera_id for c in app.camera_health()]
    if hub is not None:
        for cid in camera_ids:
            counter = [0]
            delivered.append(counter)
            t = threading.Thread(
                target=_drain,
                args=(hub, StreamKey(cid, DEFAULT_OVERLAY), stop, counter),
                name=f"vigil-bench-viewer-{cid}",
                daemon=True,
            )
            t.start()
            threads.append(t)

    started_utc = clock.wall_utc().isoformat()
    points: list[SamplePoint] = []
    try:
        points = _sample_loop(spec, app, clock, camera_ids, on_sample)
        snapshot = app.snapshot()
        issues = tuple(snapshot.health.issues)
        environment = collect_environment(app, app.nvml, extra_environment or {})
        model = snapshot.detector
    finally:
        stop.set()
        for t in threads:
            t.join(5)
        app.stop()
        shutdown_logging()

    if model is None:
        raise BenchError("the detector disappeared during the run")
    duration = points[-1].t_s if points else 0.0
    first, last = first_and_last(points, spec.duration_s, spec.window_s)
    return BenchResult(
        spec=spec,
        environment=environment,
        started_utc=started_utc,
        duration_s=duration,
        load_s=load_s,
        model={
            "name": model.name,
            "version": model.version,
            "backend": model.backend,
            "input_size_px": model.input_size_px,
            "precision": model.precision,
            "device": model.device,
            "license": model.license,
            "weights_sha256": model.weights_sha256,
        },
        vram_baseline_mb=vram_baseline,
        points=tuple(points),
        whole=summarise(points),
        first=first,
        last=last,
        stream_frames_delivered=sum(c[0] for c in delivered),
        health_issues=issues,
        notes=_notes(spec, model.device),
    )


def _notes(spec: BenchSpec, device: str) -> tuple[str, ...]:
    notes = [
        "The detector's models have a static batch of 1, so frames from several cameras are "
        "inferred one after another; cross-camera batching gains nothing yet."
    ]
    if spec.source == "synthetic":
        notes.append(
            "The source is a synthetic test pattern: detectors find nothing in it, so "
            "post-processing and NMS cost are those of an empty scene. Inference time is "
            "content-independent; post-processing time on real scenes is higher."
        )
    if spec.stream:
        notes.append("An in-process viewer consumed the annotated stream, as a browser tab would.")
    if "fell back" in device:
        notes.append(f"WARNING: this run did not use the requested device ({device}).")
    return tuple(notes)


def _sample_loop(
    spec: BenchSpec,
    app: Application,
    clock: Clock,
    camera_ids: list[str],
    on_sample: Callable[[SamplePoint], None] | None,
) -> list[SamplePoint]:
    m = app.metrics
    infer = _Cursor(m.histogram("vigil_inference_latency_ms"))
    stage = {s: _Cursor(m.histogram("vigil_stage_latency_ms", stage=s)) for s in STAGES}
    c2r = [_Cursor(m.histogram("vigil_capture_to_infer_ms", camera=c)) for c in camera_ids]
    e2e = [_Cursor(m.histogram("vigil_end_to_end_ms", camera=c)) for c in camera_ids]
    probe = SystemProbe()
    stopper = threading.Event()

    def totals() -> tuple[int, int, int, int]:
        health = app.camera_health()
        return (
            sum(h.frames_received for h in health),
            int(m.counter_total("vigil_frames_inferred_total")),
            sum(h.frames_skipped for h in health),
            sum(h.frames_dropped for h in health),
        )

    prev_totals = totals()
    start = clock.monotonic_ns()
    prev_t = start
    points: list[SamplePoint] = []
    tick = 0
    while True:
        tick += 1
        target = start + round(tick * TICK_S * NS_PER_S)
        stopper.wait(max(0.0, (target - clock.monotonic_ns()) / NS_PER_S))
        now = clock.monotonic_ns()
        t_s = (now - start) / NS_PER_S
        dt_s = (now - prev_t) / NS_PER_S
        prev_t = now
        cur = totals()
        reading = probe.read()
        gpu = app.nvml.read() if app.nvml is not None else None
        point = SamplePoint(
            t_s=t_s,
            dt_s=dt_s,
            received=cur[0] - prev_totals[0],
            inferred=cur[1] - prev_totals[1],
            skipped=cur[2] - prev_totals[2],
            dropped=cur[3] - prev_totals[3],
            infer_ms=infer.take(),
            stage_ms={s: c.take() for s, c in stage.items()},
            capture_to_result_ms=tuple(v for c in c2r for v in c.take()),
            end_to_end_ms=tuple(v for c in e2e for v in c.take()),
            cpu_percent=reading.cpu_percent,
            process_cpu_percent=reading.process_cpu_percent,
            rss_mb=reading.process_rss_mb,
            gpu_util_percent=gpu.utilization_percent if gpu else None,
            gpu_used_mb=gpu.used_mb if gpu else None,
            gpu_temp_c=gpu.temperature_c if gpu else None,
            gpu_sm_clock_mhz=gpu.sm_clock_mhz if gpu else None,
            gpu_power_w=gpu.power_w if gpu else None,
            gpu_throttle=gpu.throttle_reasons if gpu else (),
        )
        prev_totals = cur
        points.append(point)
        if on_sample is not None:
            on_sample(point)
        _abort_if_broken(app, t_s, points)
        if t_s >= spec.duration_s:
            return points


def _abort_if_broken(app: Application, t_s: float, points: list[SamplePoint]) -> None:
    """Fail loudly and early rather than spend two minutes measuring a dead system."""
    snap = app.snapshot()
    if snap.health.level is HealthLevel.FAILED:
        raise BenchError("the system failed during the run: " + "; ".join(snap.health.issues))
    if t_s >= ABORT_CHECK_S and sum(p.inferred for p in points) == 0:
        raise BenchError(
            "no frame was inferred in the first "
            f"{ABORT_CHECK_S:g} s: " + ("; ".join(snap.health.issues) or "cause unknown")
        )
