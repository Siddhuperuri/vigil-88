"""Pipeline, ingest, camera-health and evidence settings (01 §9, 07 §5)."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel


class PipelineConfig(ConfigModel):
    global_inference_fps: float = Field(default=20.0, gt=0)
    analysis_queue_size: int = Field(default=32, ge=1)
    response_queue_size: int = Field(default=128, ge=1)
    paced_queue_size: int = Field(default=4, ge=1)
    degrade_sustain_ms: int = Field(default=3000, ge=100)
    min_inference_fps: float = Field(default=2.0, gt=0)
    shutdown_timeout_ms: int = Field(default=5000, ge=100)
    worker_idle_wait_ms: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def _fps_order(self) -> Self:
        if self.min_inference_fps > self.global_inference_fps:
            raise ValueError("min_inference_fps cannot exceed global_inference_fps")
        return self


class IngestConfig(ConfigModel):
    reconnect_base_ms: int = Field(default=500, ge=1)
    reconnect_max_ms: int = Field(default=30_000, ge=1)
    reconnect_jitter_ratio: float = Field(default=0.2, ge=0.0, le=1.0)
    # 0 = unlimited once a stream has been online at least once
    max_reconnect_attempts: int = Field(default=0, ge=0)
    # opens that fail before the camera ever came online (absent / locked device)
    max_initial_open_attempts: int = Field(default=3, ge=1)
    max_consecutive_read_failures: int = Field(default=3, ge=1)
    glitch_wait_ms: int = Field(default=10, ge=0)
    watchdog_interval_ms: int = Field(default=1000, ge=50)
    stall_timeout_ms: int = Field(default=5000, ge=100)
    stall_interval_multiplier: float = Field(default=3.0, ge=1.0)

    @model_validator(mode="after")
    def _backoff_order(self) -> Self:
        if self.reconnect_base_ms > self.reconnect_max_ms:
            raise ValueError("reconnect_base_ms cannot exceed reconnect_max_ms")
        return self


class HealthConfig(ConfigModel):
    min_fps_ratio: float = Field(default=0.5, gt=0.0, le=1.0)
    fps_window_frames: int = Field(default=30, ge=5)
    min_frames_for_fps: int = Field(default=10, ge=2)
    decode_error_window_ms: int = Field(default=60_000, ge=1000)
    max_decode_errors_per_window: int = Field(default=10, ge=1)


class EvidenceConfig(ConfigModel):
    preroll_seconds: float = Field(default=5.0, gt=0)
    preroll_max_mb: int = Field(default=64, ge=1)
    postroll_seconds: float = Field(default=10.0, ge=0)
    snapshot_quality: int = Field(default=90, ge=1, le=100)
