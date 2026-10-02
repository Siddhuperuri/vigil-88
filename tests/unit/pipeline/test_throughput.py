"""Throughput of the real threaded pipeline: capture -> scheduler -> detector.

Found by the first benchmark: a 30 fps camera with a 30 fps inference target got only ~21 fps
through, although one inference took ~21 ms (capacity ~46 fps), because the inference loop
slept a fixed 20 ms after every empty poll. Service time plus the sleep exceeded the frame
period, so every other frame was overwritten unseen.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

import pytest

from vigil.config.loader import LoadedConfig
from vigil.core.clock import Clock
from vigil.domain import DetectionSet, PreparedFrame
from vigil.pipeline.app import Application, DetectorContext
from vigil.vision.backends.null import NullDetector

MakeConfig = Callable[..., LoadedConfig]


class SleepyDetector(NullDetector):
    """A detector that takes `delay_s` per frame, standing in for a model of known speed."""

    def __init__(self, clock: Clock, delay_s: float) -> None:
        super().__init__(clock)
        self._delay_s = delay_s

    def detect_batch(self, frames: Sequence[PreparedFrame]) -> Sequence[DetectionSet]:
        time.sleep(self._delay_s * len(frames))
        return super().detect_batch(frames)


# The default global ceiling is 20 fps (pipeline.global_inference_fps). These tests are about
# per-camera behaviour, so they lift it out of the way; the ceiling has its own test.
NO_GLOBAL_CAP = {"pipeline.global_inference_fps": 500.0}


def camera(*, source_fps: float, target_fps: float) -> dict[str, object]:
    return {
        "camera_id": "live-cam",
        "target_inference_fps": target_fps,
        "source": {
            "kind": "synthetic",
            "realtime": True,
            "fps": source_fps,
            "width_px": 160,
            "height_px": 120,
        },
    }


def run_for(cfg: LoadedConfig, seconds: float, delay_s: float) -> tuple[int, int, int]:
    def factory(ctx: DetectorContext) -> SleepyDetector:
        return SleepyDetector(ctx.clock, delay_s)

    app = Application(cfg.settings, cfg.paths, detector_factory=factory)
    app.start()
    try:
        threading.Event().wait(seconds)
        h = app.camera_health()[0]
        inferred = int(app.metrics.counter_total("vigil_frames_inferred_total"))
    finally:
        app.stop()
    return h.frames_received, inferred, h.frames_skipped


def test_a_camera_at_exactly_its_target_rate_loses_almost_no_frames(
    make_config: MakeConfig,
) -> None:
    # 30 fps in, 30 fps allowed, ~12 ms per inference: comfortably within capacity
    cfg = make_config(
        {
            "cameras": [camera(source_fps=30, target_fps=30)],
            "pipeline.worker_idle_wait_ms": 20,
            **NO_GLOBAL_CAP,
        }
    )  # the OLD poll interval, as a stress
    received, inferred, _ = run_for(cfg, seconds=3.0, delay_s=0.012)
    assert received >= 70
    assert inferred >= received * 0.9, f"only {inferred} of {received} frames were analysed"


def test_a_slower_inference_target_is_honoured_and_the_rest_are_skipped(
    make_config: MakeConfig,
) -> None:
    cfg = make_config({"cameras": [camera(source_fps=60, target_fps=10)], **NO_GLOBAL_CAP})
    received, inferred, skipped = run_for(cfg, seconds=3.0, delay_s=0.002)
    assert 20 <= inferred <= 40  # ~10 per second, give or take timing at the edges
    assert skipped >= received * 0.6  # the rest were deliberately not analysed
    assert inferred + skipped <= received + 1


def test_inference_is_never_the_bottleneck_when_the_detector_is_fast(
    make_config: MakeConfig,
) -> None:
    cfg = make_config({"cameras": [camera(source_fps=60, target_fps=60)], **NO_GLOBAL_CAP})
    received, inferred, _ = run_for(cfg, seconds=3.0, delay_s=0.002)  # capacity ~300 fps
    assert received >= 150 and inferred >= received * 0.9


def test_a_detector_slower_than_the_camera_caps_throughput_at_its_own_speed(
    make_config: MakeConfig,
) -> None:
    cfg = make_config({"cameras": [camera(source_fps=60, target_fps=60)], **NO_GLOBAL_CAP})
    received, inferred, skipped = run_for(cfg, seconds=3.0, delay_s=0.05)  # 20 fps capacity
    assert pytest.approx(60, abs=25) == inferred  # ~20/s x 3 s
    assert skipped > received * 0.5 and inferred < received


def test_the_default_global_ceiling_caps_total_inference_at_20_fps(make_config: MakeConfig) -> None:
    """The ceiling (pipeline.global_inference_fps, default 20) is a policy, not a bug: even a
    fast detector and a 100 fps camera are held to it. It exists to bound GPU load."""
    cfg = make_config({"cameras": [camera(source_fps=100, target_fps=100)]})  # default ceiling
    _, inferred, _ = run_for(cfg, seconds=3.0, delay_s=0.001)
    assert 40 <= inferred <= 80  # ~20 per second for 3 s
