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
    """Evidence capture (05 §6). Every bound here is a memory or disk bound, not a preference."""

    enabled: bool = True
    preroll_seconds: float = Field(default=5.0, gt=0)
    # The hard ceiling on retained evidence frames per camera. One ring spans pre-roll *and*
    # post-roll, so this is the bound that actually binds; the time window is what was asked
    # for and this is what the machine can afford. The tighter of the two always wins.
    preroll_max_mb: int = Field(default=64, ge=1)
    postroll_seconds: float = Field(default=10.0, ge=0)
    snapshot_quality: int = Field(default=90, ge=1, le=100)
    # How often the sampler stores a frame. Higher costs CPU for JPEG encoding and memory in the
    # ring; lower makes a clip choppier. It never exceeds the rate frames are analysed at.
    preroll_fps: float = Field(default=5.0, gt=0, le=30)
    # Clips need an MP4 encoder in the local OpenCV build. With this off, or with no encoder
    # available, snapshots and the JSON dumps are still captured and the incident's evidence is
    # reported partial rather than silently thinner than configured.
    clips_enabled: bool = True
    # An incident's evidence is written once its post-roll window closes, so captures are open
    # for `postroll_seconds` at a time. This bounds how many may be open at once; beyond it the
    # oldest is written early with a shorter post-roll, which is degradation, not loss.
    max_concurrent_captures: int = Field(default=8, ge=1, le=64)

    @model_validator(mode="after")
    def _preroll_fits(self) -> Self:
        if self.clips_enabled and self.preroll_fps * self.preroll_seconds < 2.0:
            raise ValueError(
                "evidence.preroll_fps and evidence.preroll_seconds must allow at least two "
                "frames of pre-roll, or no clip can be made; lower postroll or raise preroll_fps"
            )
        return self
