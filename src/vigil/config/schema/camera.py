"""Camera and source configuration, with the credential rules enforced in code (01 §10)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from vigil.config.schema.base import ConfigModel
from vigil.config.schema.calibration import CALIBRATION_REF_PATTERN
from vigil.config.schema.zones import ZONES_REF_PATTERN
from vigil.core.ids import validate_camera_id
from vigil.core.redaction import contains_inline_credentials

SourceKindName = Literal["webcam", "rtsp", "video_file", "image", "synthetic"]
WebcamBackend = Literal["auto", "msmf", "dshow"]
_RTSP_SCHEMES = ("rtsp://", "rtsps://")


class SourceConfig(ConfigModel):
    kind: SourceKindName

    # webcam
    device_index: int | None = Field(default=None, ge=0)
    backend: WebcamBackend = "auto"
    width_px: int | None = Field(default=None, ge=16)
    height_px: int | None = Field(default=None, ge=16)
    fps: float | None = Field(default=None, gt=0)
    fourcc: str | None = Field(default="MJPG", min_length=4, max_length=4)

    # rtsp: credentials are referenced by environment-variable NAME and resolved at
    # connection time. They never appear in the URL, the config or a domain object.
    url: str | None = None
    username_env: str | None = None
    password_env: str | None = None

    # video_file / image
    path: Path | None = None
    loop: bool = False

    # synthetic test pattern
    frame_count: int | None = Field(default=None, ge=1)
    seed: int = 0
    # produce frames at `fps` on the real clock with live (latest-frame) semantics, like a
    # camera; used for benchmarking. Default is the reproducible, lossless replay mode.
    realtime: bool = False

    @field_validator("url")
    @classmethod
    def _no_inline_credentials(cls, value: str | None) -> str | None:
        if value is not None and contains_inline_credentials(value):
            raise ValueError(
                "RTSP URLs must not contain credentials. Remove 'user:pass@' and set "
                "source.username_env / source.password_env to the NAMES of environment "
                "variables that hold them"
            )
        return value

    @model_validator(mode="after")
    def _kind_requirements(self) -> Self:
        match self.kind:
            case "webcam":
                if self.device_index is None:
                    raise ValueError("a webcam source requires device_index")
            case "rtsp":
                if not self.url or not self.url.startswith(_RTSP_SCHEMES):
                    raise ValueError(
                        "an rtsp source requires url starting with rtsp:// or rtsps://"
                    )
            case "video_file" | "image":
                if self.path is None:
                    raise ValueError(f"a {self.kind} source requires path")
            case "synthetic":
                pass
        return self


class CameraConfig(ConfigModel):
    camera_id: str
    name: str | None = None
    source: SourceConfig
    location: str | None = None
    priority: int = Field(default=5, ge=0, le=9)
    enabled: bool = True
    zones_ref: str | None = None
    # Names config/calibration/<ref>.calib.yaml. Without it the camera is
    # uncalibrated, GROUND_PLANE is not granted, and velocity stays in px/s.
    calibration_ref: str | None = None
    target_inference_fps: float = Field(default=5.0, gt=0)
    tags: tuple[str, ...] = ()
    # Per-camera module parameter overrides, keyed by module_id (06 §5). The innermost of the
    # three levels: descriptor defaults, then config/modules/<id>.yaml, then these. This matters
    # in practice — the same intrusion module wants a high base signal at a fence line and a low
    # one at a loading dock where people legitimately pass through.
    module_params: Mapping[str, Mapping[str, object]] = Field(default_factory=dict)
    # How much this camera's judgements are trusted, from its history of false positives. Feeds
    # the severity engine's `camera_trust` factor (06 §8); the operator sets it.
    trust_ratio: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("camera_id")
    @classmethod
    def _slug(cls, value: str) -> str:
        return validate_camera_id(value)

    @field_validator("zones_ref")
    @classmethod
    def _zones_ref_is_a_name_not_a_path(cls, value: str | None) -> str | None:
        if value is not None and not ZONES_REF_PATTERN.match(value):
            raise ValueError(
                "zones_ref must be a short name such as 'front-gate' (it selects "
                "config/zones/<name>.zones.yaml), never a path"
            )
        return value

    @field_validator("calibration_ref")
    @classmethod
    def _calibration_ref_is_a_name_not_a_path(cls, value: str | None) -> str | None:
        if value is not None and not CALIBRATION_REF_PATTERN.match(value):
            raise ValueError(
                "calibration_ref must be a short name such as 'lobby-floor' (it selects "
                "config/calibration/<name>.calib.yaml), never a path"
            )
        return value

    @property
    def display_name(self) -> str:
        return self.name or self.camera_id
