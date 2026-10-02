from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from vigil.config.schema.api import ApiConfig, is_loopback_host
from vigil.config.schema.camera import CameraConfig, SourceConfig
from vigil.config.schema.modules import TemporalConfig, TrackingConfig
from vigil.config.schema.pipeline import IngestConfig, PipelineConfig
from vigil.config.schema.severity import SeverityBands, SeverityWeights
from vigil.config.schema.vision import VisionConfig
from vigil.config.settings import Settings
from vigil.domain import SourceKind


def invalid(model: Any, match: str, **kw: object) -> None:
    with pytest.raises(ValidationError, match=match):
        model(**kw)


# ------------------------------------------------------------------ temporal (D-007)


def test_temporal_defaults_are_valid() -> None:
    t = TemporalConfig()
    assert t.l_release < t.l_activate <= t.l_bound


def test_temporal_requires_a_hysteresis_gap() -> None:
    invalid(TemporalConfig, "hysteresis", l_release=2.2, l_activate=2.2)
    invalid(TemporalConfig, "hysteresis", l_release=3.0, l_activate=2.0)


def test_temporal_activation_must_be_reachable() -> None:
    invalid(TemporalConfig, "l_bound", l_activate=9.0, l_bound=8.0)


@pytest.mark.parametrize("floor", [0.0, 0.5, 0.9, -0.1])
def test_temporal_signal_floor_must_be_strictly_inside_zero_to_half(floor: float) -> None:
    with pytest.raises(ValidationError):
        TemporalConfig(signal_floor=floor)


# ------------------------------------------------------------------ severity


def test_severity_weights_must_sum_to_one() -> None:
    SeverityWeights()
    invalid(SeverityWeights, "sum to 1.0", base=0.5)
    invalid(SeverityWeights, "sum to 1.0", base=0.0)


def test_severity_bands_must_ascend() -> None:
    SeverityBands()
    invalid(SeverityBands, "ascending", LOW=50.0, MODERATE=40.0)
    invalid(SeverityBands, "ascending", HIGH=85.0, CRITICAL=85.0)


# ------------------------------------------------------------------ vision / pipeline / ingest


def test_vision_input_size_must_be_a_listed_tier_largest_first() -> None:
    VisionConfig()
    invalid(VisionConfig, "resolution_tiers_px", input_size_px=480)
    invalid(VisionConfig, "largest first", resolution_tiers_px=(416, 512, 640))


@pytest.mark.parametrize("batch", [0, 65])
def test_vision_batch_bounds(batch: int) -> None:
    with pytest.raises(ValidationError):
        VisionConfig(max_batch_size=batch)


def test_pipeline_min_fps_cannot_exceed_global() -> None:
    invalid(PipelineConfig, "min_inference_fps", min_inference_fps=30.0, global_inference_fps=20.0)


def test_ingest_backoff_must_be_ordered() -> None:
    invalid(IngestConfig, "reconnect_base_ms", reconnect_base_ms=60_000, reconnect_max_ms=1000)


def test_tracking_velocity_history_must_fit() -> None:
    invalid(TrackingConfig, "history_length", min_history_for_velocity=100, history_length=90)


def test_unknown_keys_are_rejected_in_every_section() -> None:
    for model in (VisionConfig, TemporalConfig, PipelineConfig, IngestConfig, ApiConfig):
        with pytest.raises(ValidationError, match="Extra inputs"):
            model(definitely_not_a_key=1)


def test_models_are_frozen() -> None:
    with pytest.raises(ValidationError):
        VisionConfig().max_batch_size = 9  # type: ignore[misc]


# ------------------------------------------------------------------ cameras and credentials


def src(**kw: object) -> SourceConfig:
    return SourceConfig(**kw)  # type: ignore[arg-type]


def test_webcam_requires_device_index() -> None:
    src(kind="webcam", device_index=0)
    invalid(SourceConfig, "device_index", kind="webcam")
    invalid(SourceConfig, "device_index", kind="webcam", device_index=-1)


def test_rtsp_requires_an_rtsp_url() -> None:
    src(kind="rtsp", url="rtsp://10.0.0.5:554/stream")
    src(kind="rtsp", url="rtsps://10.0.0.5/stream")
    invalid(SourceConfig, "rtsp", kind="rtsp")
    invalid(SourceConfig, "rtsp", kind="rtsp", url="http://10.0.0.5/stream")


def test_rtsp_urls_with_inline_credentials_are_rejected_and_the_fix_is_named() -> None:
    with pytest.raises(ValidationError) as e:
        src(kind="rtsp", url="rtsp://admin:hunter2@10.0.0.5/stream")
    msg = str(e.value)
    assert "username_env" in msg and "password_env" in msg
    assert "hunter2" not in msg  # the rejected credential is never echoed


def test_rtsp_credentials_are_referenced_by_variable_name() -> None:
    s = src(
        kind="rtsp", url="rtsp://10.0.0.5/s", username_env="CAM_N_USER", password_env="CAM_N_PASS"
    )
    assert s.password_env == "CAM_N_PASS"  # a name, never a value


def test_file_sources_require_a_path() -> None:
    invalid(SourceConfig, "path", kind="video_file")
    invalid(SourceConfig, "path", kind="image")


def test_fourcc_must_be_four_characters() -> None:
    invalid(SourceConfig, "fourcc", kind="webcam", device_index=0, fourcc="MJPEG")


def test_camera_id_is_validated_as_a_slug() -> None:
    CameraConfig(camera_id="gate-north", source=src(kind="synthetic"))
    for bad in ("Gate", "x", "has space", "-x"):
        with pytest.raises(ValidationError):
            CameraConfig(camera_id=bad, source=src(kind="synthetic"))


def test_camera_display_name_falls_back_to_id() -> None:
    assert CameraConfig(camera_id="cam-a", source=src(kind="synthetic")).display_name == "cam-a"
    named = CameraConfig(camera_id="cam-a", name="Front", source=src(kind="synthetic"))
    assert named.display_name == "Front"


def test_camera_priority_and_fps_bounds() -> None:
    for bad in ({"priority": 10}, {"priority": -1}, {"target_inference_fps": 0}):
        with pytest.raises(ValidationError):
            CameraConfig(camera_id="cam-a", source=src(kind="synthetic"), **bad)  # type: ignore[arg-type]


def test_duplicate_camera_ids_are_rejected() -> None:
    cams = [{"camera_id": "cam-a", "source": {"kind": "synthetic"}}] * 2
    with pytest.raises(ValidationError, match="duplicate camera_id"):
        Settings.model_validate({"cameras": cams})


def test_config_source_kinds_match_domain_source_kinds() -> None:
    """Config cannot import the domain, so the two lists are kept in sync by this test."""
    from typing import get_args

    from vigil.config.schema.camera import SourceKindName

    assert set(get_args(SourceKindName)) == {k.value for k in SourceKind}


# ------------------------------------------------------------------ api security (01 §10)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST", "::1", "127.5.5.5"])
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host)
    ApiConfig(bind_host=host)  # no auth needed


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])  # noqa: S104
def test_non_loopback_hosts(host: str) -> None:
    assert not is_loopback_host(host)


def test_non_loopback_bind_requires_authentication() -> None:
    invalid(ApiConfig, "not loopback", bind_host="0.0.0.0")  # noqa: S104


def test_non_loopback_bind_requires_a_long_token() -> None:
    invalid(ApiConfig, "at least 32", bind_host="0.0.0.0", auth={"enabled": True, "token": "short"})  # noqa: S104
    invalid(ApiConfig, "at least 32", bind_host="0.0.0.0", auth={"enabled": True})  # noqa: S104
    ok = ApiConfig(bind_host="0.0.0.0", auth={"enabled": True, "token": "x" * 32})  # noqa: S104
    assert ok.auth.token is not None and "x" * 32 not in repr(ok)


def test_secret_token_never_appears_in_repr_or_json() -> None:
    cfg = ApiConfig(bind_host="0.0.0.0", auth={"enabled": True, "token": "y" * 40})  # noqa: S104
    assert "y" * 40 not in repr(cfg)
    assert "y" * 40 not in cfg.model_dump_json()
