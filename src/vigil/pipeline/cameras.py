"""Config -> domain mapping for cameras. Config is below domain in the layering, so the
translation lives here, above both."""

from __future__ import annotations

from vigil.config.schema.camera import CameraConfig, SourceConfig
from vigil.domain.camera import Camera, SourceOption, SourceSpec
from vigil.domain.enums import SourceKind


def _options(src: SourceConfig) -> dict[str, SourceOption]:
    candidates: dict[str, SourceOption | None]
    match src.kind:
        case "webcam":
            candidates = {
                "device_index": src.device_index,
                "backend": src.backend,
                "width_px": src.width_px,
                "height_px": src.height_px,
                "fps": src.fps,
                "fourcc": src.fourcc,
            }
        case "synthetic":
            candidates = {
                "width_px": src.width_px,
                "height_px": src.height_px,
                "fps": src.fps,
                "frame_count": src.frame_count,
                "seed": src.seed,
                "realtime": src.realtime,
            }
        case "rtsp":
            # Names of environment variables, never their values.
            candidates = {"username_env": src.username_env, "password_env": src.password_env}
        case "video_file" | "image":
            candidates = {"loop": src.loop}
    return {k: v for k, v in candidates.items() if v is not None}


def _locator(src: SourceConfig) -> str:
    match src.kind:
        case "webcam":
            return f"webcam:{src.device_index}"
        case "rtsp":
            return str(src.url)
        case "video_file" | "image":
            return f"{src.kind}:{src.path}"
        case "synthetic":
            return "synthetic"


def camera_from_config(cfg: CameraConfig) -> Camera:
    return Camera(
        camera_id=cfg.camera_id,
        name=cfg.display_name,
        source=SourceSpec(SourceKind(cfg.source.kind), _locator(cfg.source), _options(cfg.source)),
        location=cfg.location,
        priority=cfg.priority,
        enabled=cfg.enabled,
        zones_ref=cfg.zones_ref,
        target_inference_fps=cfg.target_inference_fps,
        tags=cfg.tags,
    )
