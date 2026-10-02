"""Camera, its source description and its health snapshot (05 §4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from vigil.core.ids import validate_camera_id
from vigil.core.redaction import contains_inline_credentials
from vigil.domain._validate import (
    frozen_mapping,
    require_non_empty,
    require_non_negative,
    require_positive,
    require_utc,
)
from vigil.domain.enums import CameraState, SourceKind

SourceOption = str | int | float | bool

MIN_PRIORITY = 0
MAX_PRIORITY = 9


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Where frames come from. NEVER carries credentials (05 §4)."""

    kind: SourceKind
    locator: str
    options: Mapping[str, SourceOption]

    def __post_init__(self) -> None:
        require_non_empty("SourceSpec.locator", self.locator)
        if contains_inline_credentials(self.locator):
            raise ValueError("SourceSpec.locator must not contain inline credentials")
        object.__setattr__(self, "options", frozen_mapping(self.options))


@dataclass(frozen=True, slots=True)
class Camera:
    camera_id: str
    name: str
    source: SourceSpec
    location: str | None
    priority: int
    enabled: bool
    zones_ref: str | None
    target_inference_fps: float
    tags: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_camera_id(self.camera_id)
        require_non_empty("Camera.name", self.name)
        if not MIN_PRIORITY <= self.priority <= MAX_PRIORITY:
            raise ValueError(f"priority must be in [{MIN_PRIORITY}, {MAX_PRIORITY}]")
        require_positive("Camera.target_inference_fps", self.target_inference_fps)


@dataclass(frozen=True, slots=True)
class CameraHealth:
    """`frames_skipped` is intentional (latest-only); `frames_dropped` is a failure (05 §4)."""

    camera_id: str
    state: CameraState
    since_wall_utc: datetime
    stream_epoch: int
    reconnect_attempts: int
    last_frame_wall_utc: datetime | None
    measured_fps: float | None
    frames_received: int
    frames_skipped: int
    frames_dropped: int
    decode_errors: int
    detail: str | None

    def __post_init__(self) -> None:
        validate_camera_id(self.camera_id)
        require_utc("CameraHealth.since_wall_utc", self.since_wall_utc)
        if self.last_frame_wall_utc is not None:
            require_utc("CameraHealth.last_frame_wall_utc", self.last_frame_wall_utc)
        for name in (
            "stream_epoch",
            "reconnect_attempts",
            "frames_received",
            "frames_skipped",
            "frames_dropped",
            "decode_errors",
        ):
            require_non_negative(name, getattr(self, name))
        if self.measured_fps is not None:
            require_non_negative("measured_fps", self.measured_fps)


@dataclass(frozen=True, slots=True)
class CameraStateChanged:
    """Published on the event bus for every camera health transition."""

    camera_id: str
    old: CameraState
    new: CameraState
    detail: str | None
    wall_utc: datetime
