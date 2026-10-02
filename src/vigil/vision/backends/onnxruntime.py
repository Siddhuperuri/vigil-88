"""ONNX Runtime detector backend (YOLOX family). Satisfies the Detector protocol.

Loading is strict:
  * the weights file must be listed in MANIFEST.json and match its recorded SHA-256;
  * the CUDA arena gets a hard memory ceiling (`vision.vram_budget_mb`), and measured usage
    after load is checked against the same budget;
  * ONNX Runtime silently falls back to CPU if a CUDA provider fails to initialise, so the
    *active* providers are inspected rather than trusted;
  * a fallback is allowed only if `vision.allow_cpu_fallback` says so, and is always loud.

These YOLOX exports have a static batch of 1, so a batch of N frames is N sequential runs.
Cross-camera batching gains nothing until a model with a dynamic batch axis is used.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol

import numpy as np

from vigil.config.schema.vision import VisionConfig
from vigil.core.capabilities import Capability
from vigil.core.clock import Clock
from vigil.core.errors import CapabilityError, InferenceError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import BYTES_PER_MIB, ns_to_ms
from vigil.domain.detection import DetectionSet, ModelDescriptor, PreparedFrame
from vigil.observability.metrics import MetricsRegistry
from vigil.observability.probes import NvmlProbe
from vigil.vision.device import (
    CPU_PROVIDER,
    CUDA_PROVIDER,
    ResolvedDevice,
    available_providers,
    prepare_cuda_runtime,
    resolve_device,
)
from vigil.vision.labels import COCO_80, LabelMapper
from vigil.vision.models import KNOWN_MODELS, VerifiedModel, verify_model
from vigil.vision.postprocess import YoloxDecoder, postprocess_yolox
from vigil.vision.preprocess import F32, letterbox_bgr

_CHANNELS = 3
_WARMUP_RUNS = 2
_STATIC_RANK = 4
_BLANK_VALUE = 114.0  # the letterbox padding value, so warm-up looks like real input


class Session(Protocol):
    """The slice of an onnxruntime InferenceSession used here; tests substitute a fake."""

    @property
    def input_size_px(self) -> int: ...

    @property
    def active_providers(self) -> tuple[str, ...]: ...

    def run(self, batch: F32) -> F32: ...


SessionFactory = Callable[[Path, str, VisionConfig], Session]


class _OrtSession:
    def __init__(self, path: Path, kind: str, cfg: VisionConfig) -> None:
        import onnxruntime as ort

        if kind == "cuda":
            prepare_cuda_runtime()  # sub-libraries are loaded by name: they must be on PATH
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.log_severity_level = 3  # errors only: failures surface as exceptions
        if cfg.cpu_threads:
            options.intra_op_num_threads = cfg.cpu_threads

        providers: list[str | tuple[str, dict[str, object]]]
        if kind == "cuda":
            providers = [
                (
                    CUDA_PROVIDER,
                    {
                        "device_id": 0,
                        "gpu_mem_limit": cfg.vram_budget_mb * BYTES_PER_MIB,
                        "arena_extend_strategy": "kSameAsRequested",
                        "use_tf32": 1,
                    },
                ),
                CPU_PROVIDER,
            ]
        else:
            providers = [CPU_PROVIDER]

        self._session = ort.InferenceSession(str(path), sess_options=options, providers=providers)
        # By default the Python layer retries a failed run on CPU *silently*, after which the
        # session still reports success. Turn that off: a failure must reach our own policy
        # (`vision.allow_cpu_fallback`), which logs and counts it.
        self._session.disable_fallback()
        meta = self._session.get_inputs()[0]
        self._input_name: str = meta.name
        shape = meta.shape
        if len(shape) != _STATIC_RANK or not all(isinstance(d, int) for d in shape[1:]):
            raise CapabilityError(f"model input shape {shape} is not a static (N, 3, H, W) tensor")
        if shape[1] != _CHANNELS or shape[2] != shape[3]:
            raise CapabilityError(f"model input shape {shape} is not a square 3-channel image")
        self._size = int(shape[2])

    @property
    def input_size_px(self) -> int:
        return self._size

    @property
    def active_providers(self) -> tuple[str, ...]:
        return tuple(self._session.get_providers())

    def run(self, batch: F32) -> F32:
        out = self._session.run(None, {self._input_name: batch})[0]
        return np.asarray(out, dtype=np.float32)


def _default_session_factory(path: Path, kind: str, cfg: VisionConfig) -> Session:
    return _OrtSession(path, kind, cfg)


class OnnxDetector:
    def __init__(
        self,
        *,
        cfg: VisionConfig,
        models_dir: Path,
        clock: Clock,
        logger: LoggerLike,
        metrics: MetricsRegistry | None = None,
        nvml: NvmlProbe | None = None,
        providers: Sequence[str] | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        if cfg.weights is None:
            raise CapabilityError("the onnxruntime backend requires vision.weights")
        if cfg.precision == "fp16":
            raise CapabilityError(
                "vision.precision 'fp16' is not supported by the ONNX backend yet; "
                "use 'auto' or 'fp32'"
            )
        self._cfg = cfg
        self._clock = clock
        self._log = logger.bind(stage="infer", backend="onnxruntime")
        self._metrics = metrics
        self._nvml = nvml
        self._factory = session_factory or _default_session_factory
        self._session: Session | None = None

        self._model: VerifiedModel = verify_model(models_dir, cfg.weights)
        self._log.info(
            "model integrity verified", file=self._model.entry.file, sha256=self._model.entry.sha256
        )

        wanted = resolve_device(
            cfg.device,
            allow_cpu_fallback=cfg.allow_cpu_fallback,
            providers=available_providers() if providers is None else providers,
        )
        self._device = wanted
        self._load(wanted)
        session = self._require_session()
        self._size = session.input_size_px
        self._check_input_size()

        self._mapper = LabelMapper(COCO_80, cfg.label_map, metrics)
        self._decoder = YoloxDecoder(self._size, self._mapper.num_classes)
        self._descriptor = self._build_descriptor()
        self._log.info(
            "detector ready",
            device=self._descriptor.device,
            size_px=self._size,
            precision=self._descriptor.precision,
            reason=self._device.reason,
        )

    # ------------------------------------------------------------------ loading

    def _require_session(self) -> Session:
        if self._session is None:
            raise InferenceError("the detector is closed")
        return self._session

    def _load(self, wanted: ResolvedDevice) -> None:
        if wanted.kind == "cpu":
            self._session = self._create("cpu")
            self._warm(self._session)
            return
        used_before = self._gpu_used_mb()
        try:
            session = self._create("cuda")
        except CapabilityError:
            raise
        except Exception as exc:  # noqa: BLE001 - ORT raises its own types
            self._cuda_failed(f"{type(exc).__name__}: {exc}")
            return
        if CUDA_PROVIDER not in session.active_providers:
            self._cuda_failed(
                f"onnxruntime silently fell back (active providers: {session.active_providers})"
            )
            return
        try:
            self._warm(session)  # the first real run is where cuDNN/CUDA problems appear
        except Exception as exc:  # noqa: BLE001 - ORT raises its own types
            self._cuda_failed(f"warm-up run failed: {type(exc).__name__}: {str(exc)[:300]}")
            return
        self._session = session
        self._enforce_vram_budget(used_before)

    def _create(self, kind: str) -> Session:
        try:
            return self._factory(self._model.path, kind, self._cfg)
        except CapabilityError:
            raise
        except Exception as exc:
            if kind == "cuda":
                raise
            raise CapabilityError(
                f"failed to load {self._model.entry.file} on CPU: {type(exc).__name__}: {exc}"
            ) from exc

    def _cuda_failed(self, why: str) -> None:
        if not self._cfg.allow_cpu_fallback:
            raise CapabilityError(
                f"CUDA inference could not be started and vision.allow_cpu_fallback is false: {why}"
            )
        self._log.error("CUDA unavailable, falling back to CPU", reason=why)
        if self._metrics is not None:
            self._metrics.counter("vigil_detector_fallbacks_total", phase="load").inc()
        self._device = ResolvedDevice("cpu", f"fell back from CUDA: {why}", fell_back=True)
        self._session = self._create("cpu")
        self._warm(self._session)

    def _warm(self, session: Session) -> None:
        """Run on a blank frame so cuDNN autotuning and allocator warm-up are not charged to
        the first real frame (04 §3.1)."""
        size = session.input_size_px
        blank = np.full((1, _CHANNELS, size, size), _BLANK_VALUE, dtype=np.float32)
        start = self._clock.monotonic_ns()
        for _ in range(_WARMUP_RUNS):
            session.run(blank)
        if self._metrics is not None:
            self._metrics.gauge("vigil_detector_warmup_ms").set(
                ns_to_ms(self._clock.monotonic_ns() - start)
            )

    def _gpu_used_mb(self) -> float | None:
        reading = self._nvml.read() if self._nvml is not None else None
        return reading.used_mb if reading is not None else None

    def _enforce_vram_budget(self, used_before: float | None) -> None:
        used_after = self._gpu_used_mb()
        if used_before is None or used_after is None:
            return
        delta = used_after - used_before
        if self._metrics is not None:
            self._metrics.gauge("vigil_detector_vram_mb").set(max(delta, 0.0))
        self._log.info(
            "detector VRAM after load", delta_mb=round(delta, 1), budget_mb=self._cfg.vram_budget_mb
        )
        if delta > self._cfg.vram_budget_mb:
            self._session = None  # release the model so its memory is freed
            raise CapabilityError(
                f"loading the model took about {delta:.0f} MB of GPU memory, over "
                f"vision.vram_budget_mb={self._cfg.vram_budget_mb}"
            )

    def _check_input_size(self) -> None:
        requested = self._cfg.input_size_px
        if requested is not None and requested != self._size:
            self._session = None
            raise CapabilityError(
                f"{self._model.entry.file} has a fixed {self._size}px input but "
                f"vision.input_size_px is {requested}. Remove it to use the model's native size, "
                "or choose a model exported at the size you want"
            )

    def _build_descriptor(self) -> ModelDescriptor:
        if self._device.kind == "cuda":
            gpu = self._nvml.read() if self._nvml is not None else None
            label = f"cuda:0 {gpu.name}" if gpu is not None else "cuda:0"
            precision = "fp32+tf32"
        else:
            label = "cpu (fell back from cuda)" if self._device.fell_back else "cpu"
            precision = "fp32"
        stem = self._model.entry.file.removesuffix(".onnx")
        spec = KNOWN_MODELS.get(stem)
        return ModelDescriptor(
            name=stem,
            version=spec.version if spec else "unversioned",
            backend="onnxruntime",
            weights_sha256=self._model.entry.sha256,
            input_size_px=self._size,
            precision=precision,
            device=label,
            license=self._model.entry.license,
        )

    # ------------------------------------------------------------------ Detector protocol

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def provides(self) -> frozenset[Capability]:
        return frozenset({Capability.DETECTION})

    @property
    def device_reason(self) -> str:
        return self._device.reason

    def warmup(self) -> None:
        self._require_session()  # already warmed during load; kept for protocol symmetry

    def detect_batch(self, frames: Sequence[PreparedFrame]) -> Sequence[DetectionSet]:
        self._require_session()
        return [self._detect_one(f) for f in frames]

    def _detect_one(self, prepared: PreparedFrame) -> DetectionSet:
        frame = prepared.frame
        meta = frame.meta
        start = self._clock.monotonic_ns()
        try:
            pixels = np.frombuffer(frame.pixels.buffer(), dtype=np.uint8)
            image = pixels.reshape(meta.height_px, meta.width_px, frame.pixels.channels)
        except ValueError as exc:
            raise InferenceError(f"cannot read frame pixels: {exc}") from exc
        chw, transform = letterbox_bgr(image, self._size)
        pre = self._clock.monotonic_ns()
        raw = self._run(chw)
        infer = self._clock.monotonic_ns()
        detections = postprocess_yolox(
            raw,
            self._decoder,
            transform,
            self._mapper,
            score_threshold=self._cfg.min_detection_confidence_ratio,
            iou_threshold=self._cfg.nms_iou_ratio,
            max_detections=self._cfg.max_detections,
        )
        end = self._clock.monotonic_ns()
        self._record(pre - start, infer - pre, end - infer)
        return DetectionSet(meta, tuple(detections), self._descriptor, ns_to_ms(end - start))

    def _record(self, pre_ns: int, infer_ns: int, post_ns: int) -> None:
        if self._metrics is None:
            return
        for stage, ns in (("preprocess", pre_ns), ("infer", infer_ns), ("postprocess", post_ns)):
            self._metrics.histogram("vigil_stage_latency_ms", stage=stage).observe(ns_to_ms(ns))

    def _run(self, chw: F32) -> F32:
        session = self._require_session()
        batch = chw[None]  # a view: no copy
        try:
            return session.run(batch)
        except Exception as exc:
            if self._device.kind == "cuda" and self._cfg.allow_cpu_fallback:
                return self._fallback_run(batch, exc)
            raise InferenceError(f"inference failed: {type(exc).__name__}: {exc}") from exc

    def _fallback_run(self, batch: F32, original: Exception) -> F32:
        """A CUDA run failed (e.g. out of memory): continue on CPU, loudly."""
        why = f"{type(original).__name__}: {original}"
        self._log.error("CUDA inference failed, switching to CPU", error=why)
        if self._metrics is not None:
            self._metrics.counter("vigil_detector_fallbacks_total", phase="run").inc()
        self._session = None  # free the failed CUDA session before building the CPU one
        self._device = ResolvedDevice("cpu", f"fell back from CUDA: {why}", fell_back=True)
        session = self._create("cpu")
        self._session = session
        self._descriptor = replace(
            self._descriptor, device="cpu (fell back from cuda)", precision="fp32"
        )
        try:
            return session.run(batch)
        except Exception as exc:
            raise InferenceError(
                f"inference failed on CUDA and again on CPU: {type(exc).__name__}: {exc}"
            ) from exc

    def close(self) -> None:
        """Idempotent. Dropping the session is what releases the model's (GPU) memory."""
        self._session = None
