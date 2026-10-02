"""API response models. Deliberately separate from domain objects (02 §2): the wire format
changes for client reasons, the domain for engine reasons."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from vigil.domain.camera import CameraHealth
from vigil.domain.detection import DetectionSet, ModelDescriptor
from vigil.domain.metric import SystemMetric
from vigil.observability.metrics import HistogramSummary


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class ModelOut(_Out):
    name: str
    version: str
    backend: str
    input_size_px: int | None
    precision: str
    device: str
    license: str | None
    weights_sha256: str | None

    @classmethod
    def of(cls, m: ModelDescriptor) -> ModelOut:
        return cls(
            name=m.name,
            version=m.version,
            backend=m.backend,
            input_size_px=m.input_size_px,
            precision=m.precision,
            device=m.device,
            license=m.license,
            weights_sha256=m.weights_sha256,
        )


class CameraOut(_Out):
    camera_id: str
    state: str
    detail: str | None
    stream_epoch: int
    measured_fps: float | None
    frames_received: int
    frames_skipped: int  # intentionally not analysed (inference slower than capture): normal
    frames_dropped: int  # lost to a full queue: a problem
    decode_errors: int
    reconnect_attempts: int
    last_frame_utc: str | None

    @classmethod
    def of(cls, h: CameraHealth) -> CameraOut:
        return cls(
            camera_id=h.camera_id,
            state=h.state.value,
            detail=h.detail,
            stream_epoch=h.stream_epoch,
            measured_fps=h.measured_fps,
            frames_received=h.frames_received,
            frames_skipped=h.frames_skipped,
            frames_dropped=h.frames_dropped,
            decode_errors=h.decode_errors,
            reconnect_attempts=h.reconnect_attempts,
            last_frame_utc=h.last_frame_wall_utc.isoformat() if h.last_frame_wall_utc else None,
        )


class BoxOut(_Out):
    x1: float
    y1: float
    x2: float
    y2: float


class DetectionOut(_Out):
    object_class: str
    native_label: str
    confidence: float
    bbox: BoxOut  # SOURCE-frame pixels


class DetectionsOut(_Out):
    camera_id: str
    stream_epoch: int
    frame_index: int
    width_px: int
    height_px: int
    wall_utc: str
    inference_latency_ms: float
    model: ModelOut
    detections: list[DetectionOut]

    @classmethod
    def of(cls, r: DetectionSet) -> DetectionsOut:
        m = r.frame_meta
        return cls(
            camera_id=m.camera_id,
            stream_epoch=m.stream_epoch,
            frame_index=m.frame_index,
            width_px=m.width_px,
            height_px=m.height_px,
            wall_utc=m.wall_utc.isoformat(),
            inference_latency_ms=r.inference_latency_ms,
            model=ModelOut.of(r.model),
            detections=[
                DetectionOut(
                    object_class=d.object_class.value,
                    native_label=d.native_label,
                    confidence=d.confidence_ratio,
                    bbox=BoxOut(x1=d.bbox.x1, y1=d.bbox.y1, x2=d.bbox.x2, y2=d.bbox.y2),
                )
                for d in r.detections
            ],
        )


class CapabilityOut(_Out):
    capability: str
    available: bool
    provider: str | None
    how_to_provide: str | None


class HealthOut(_Out):
    state: str
    level: str
    issues: list[str]
    uptime_ms: float | None
    detector: ModelOut | None
    capabilities: list[CapabilityOut]


class LatencyOut(_Out):
    count: int
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None

    @classmethod
    def of(cls, s: HistogramSummary) -> LatencyOut:
        return cls(
            count=s.count,
            mean_ms=s.mean,
            p50_ms=s.p50,
            p95_ms=s.p95,
            p99_ms=s.p99,
            max_ms=s.maximum,
        )


class SystemOut(_Out):
    cpu_percent: float
    memory_used_mb: float
    process_rss_mb: float
    process_cpu_percent: float
    gpu_utilization_percent: float | None  # None = not measured (never 0)
    gpu_memory_used_mb: float | None
    gpu_temperature_c: float | None
    gpu_sm_clock_mhz: float | None
    gpu_power_w: float | None
    gpu_throttle_reasons: list[str]

    @classmethod
    def of(cls, m: SystemMetric) -> SystemOut:
        return cls(
            cpu_percent=m.cpu_percent,
            memory_used_mb=m.memory_used_mb,
            process_rss_mb=m.process_rss_mb,
            process_cpu_percent=m.process_cpu_percent,
            gpu_utilization_percent=m.gpu_utilization_percent,
            gpu_memory_used_mb=m.gpu_memory_used_mb,
            gpu_temperature_c=m.gpu_temperature_c,
            gpu_sm_clock_mhz=m.gpu_sm_clock_mhz,
            gpu_power_w=m.gpu_power_w,
            gpu_throttle_reasons=list(m.gpu_throttle_reasons),
        )


class MetricsOut(_Out):
    system: SystemOut | None
    pipeline_fps: float | None
    inference_latency: LatencyOut
    stage_latency: dict[str, LatencyOut]
    capture_to_result: dict[str, LatencyOut]
    end_to_end: dict[str, LatencyOut]
    counters: dict[str, float]
    cameras: list[CameraOut]
