"""Root settings model. The complete schema; see docs/CONFIG.md (generated) for every key."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import Field, model_validator

from vigil.config.schema.api import ApiConfig
from vigil.config.schema.base import ConfigModel
from vigil.config.schema.camera import CameraConfig
from vigil.config.schema.modules import (
    ModulesConfig,
    SceneConfig,
    TemporalConfig,
    TrackingConfig,
    VerificationConfig,
)
from vigil.config.schema.pipeline import EvidenceConfig, HealthConfig, IngestConfig, PipelineConfig
from vigil.config.schema.retention import RetentionConfig
from vigil.config.schema.severity import SeverityConfig
from vigil.config.schema.system import LoggingConfig, ObservabilityConfig, PathsConfig
from vigil.config.schema.vision import VisionConfig


class Settings(ConfigModel):
    version: Literal[1] = 1
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    scene: SceneConfig = Field(default_factory=SceneConfig)
    temporal: TemporalConfig = Field(default_factory=TemporalConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    severity: SeverityConfig = Field(default_factory=SeverityConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    modules: ModulesConfig = Field(default_factory=ModulesConfig)
    cameras: tuple[CameraConfig, ...] = ()

    @model_validator(mode="after")
    def _unique_camera_ids(self) -> Self:
        seen: set[str] = set()
        for cam in self.cameras:
            if cam.camera_id in seen:
                raise ValueError(f"duplicate camera_id {cam.camera_id!r}")
            seen.add(cam.camera_id)
        return self

    def config_hash(self) -> str:
        """sha256 of the effective config. Secrets serialise masked, so they never enter it."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
