from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.onnx_models import PlantedBox, write_model
from vigil.bench.report import (
    ReportError,
    load_results,
    render_markdown,
    result_to_dict,
    save_result,
    write_report,
)
from vigil.bench.runner import BenchError, BenchResult, run_benchmark
from vigil.bench.spec import SUSTAINED_MIN_S, BenchSpec
from vigil.bench.stats import (
    SamplePoint,
    change_percent,
    dist,
    first_and_last,
    summarise,
    value_range,
    window,
)
from vigil.core.errors import ConfigError


def point(
    t: float,
    *,
    inferred: int = 30,
    infer_ms: tuple[float, ...] = (10.0,),
    gpu_util: float | None = 50.0,
    sm: float | None = 1500.0,
    temp: float | None = 60.0,
    throttle: tuple[str, ...] = (),
    dt: float = 1.0,
    **kw: object,
) -> SamplePoint:
    base: dict[str, object] = dict(
        t_s=t,
        dt_s=dt,
        received=inferred + 5,
        inferred=inferred,
        skipped=5,
        dropped=0,
        infer_ms=infer_ms,
        stage_ms={"infer": infer_ms},
        capture_to_result_ms=(12.0,),
        end_to_end_ms=(20.0,),
        cpu_percent=20.0,
        process_cpu_percent=60.0,
        rss_mb=500.0,
        gpu_util_percent=gpu_util,
        gpu_used_mb=1500.0 if gpu_util is not None else None,
        gpu_temp_c=temp,
        gpu_sm_clock_mhz=sm,
        gpu_power_w=30.0 if gpu_util is not None else None,
        gpu_throttle=throttle,
    )
    return SamplePoint(**{**base, **kw})  # type: ignore[arg-type]


# ------------------------------------------------------------------ statistics


def test_dist_percentiles() -> None:
    d = dist(float(v) for v in range(1, 101))
    assert (d.count, d.p50, d.p95, d.p99, d.maximum) == (100, 50.0, 95.0, 99.0, 100.0)
    assert d.mean == pytest.approx(50.5)


def test_an_empty_distribution_is_unmeasured_not_zero() -> None:
    d = dist([])
    assert d.count == 0 and d.p50 is None and d.mean is None and d.maximum is None


def test_value_range_excludes_missing_values_instead_of_counting_them_as_zero() -> None:
    r = value_range([None, 10.0, None, 30.0])
    assert (r.mean, r.minimum, r.maximum) == (20.0, 10.0, 30.0)
    assert value_range([None, None]).mean is None


def test_summarise_computes_fps_exactly() -> None:
    points = [point(float(i + 1), inferred=n) for i, n in enumerate([10, 20, 30, 40])]
    s = summarise(points)
    assert s.inferred == 100 and s.seconds == 4.0
    assert s.fps_mean == pytest.approx(25.0)
    assert s.fps_median == pytest.approx(25.0)  # median of 10, 20, 30, 40
    assert s.fps_low == 10.0  # slowest 10% of seconds


def test_fps_uses_the_actual_interval_not_an_assumed_one_second() -> None:
    s = summarise([point(1.0, inferred=30, dt=2.0)])
    assert s.fps_mean == pytest.approx(15.0)


def test_summarise_with_no_gpu_leaves_every_gpu_field_unmeasured() -> None:
    s = summarise([point(1.0, gpu_util=None, sm=None, temp=None)])
    assert s.gpu_util_percent.mean is None and s.gpu_sm_clock_mhz.maximum is None
    assert s.gpu_temp_c.minimum is None and s.throttled_seconds == 0


def test_throttling_is_counted_in_seconds_and_named() -> None:
    pts = [
        point(1.0),
        point(2.0, throttle=("sw_power_cap",)),
        point(3.0, throttle=("sw_power_cap", "hw_thermal_slowdown")),
    ]
    s = summarise(pts)
    assert s.throttled_seconds == 2
    assert s.throttle_reasons == ("hw_thermal_slowdown", "sw_power_cap")


def test_windows_select_by_the_interval_end() -> None:
    pts = [point(float(t)) for t in range(1, 11)]
    assert [p.t_s for p in window(pts, 0.0, 3.0)] == [1.0, 2.0, 3.0]
    assert [p.t_s for p in window(pts, 7.0, 10.0)] == [8.0, 9.0, 10.0]


def test_first_and_last_windows_show_a_slowdown_under_sustained_load() -> None:
    fast = [point(float(t), inferred=60, infer_ms=(8.0,), sm=1800.0) for t in range(1, 6)]
    slow = [point(float(t), inferred=40, infer_ms=(14.0,), sm=900.0) for t in range(6, 11)]
    first, last = first_and_last(fast + slow, duration_s=10.0, window_s=5.0)
    assert first.fps_mean == 60.0 and last.fps_mean == 40.0
    assert first.infer_ms.p50 == 8.0 and last.infer_ms.p50 == 14.0
    assert first.gpu_sm_clock_mhz.mean == 1800.0 and last.gpu_sm_clock_mhz.mean == 900.0
    assert change_percent(first.fps_mean, last.fps_mean) == pytest.approx(-33.333, abs=0.01)


def test_change_percent_handles_missing_and_zero() -> None:
    assert change_percent(None, 5.0) is None and change_percent(5.0, None) is None
    assert change_percent(0.0, 5.0) is None
    assert change_percent(100.0, 150.0) == 50.0


def test_summarising_nothing_does_not_crash() -> None:
    s = summarise([])
    assert s.inferred == 0 and s.fps_mean is None and s.fps_median is None


# ------------------------------------------------------------------ spec


def spec(**kw: object) -> BenchSpec:
    base: dict[str, object] = dict(name="t", detector="yolox_s", device="cuda", source="synthetic")
    return BenchSpec(**{**base, **kw})  # type: ignore[arg-type]


def test_a_default_spec_is_a_valid_sustained_run() -> None:
    s = spec()
    s.validate()
    assert s.sustained and s.duration_s >= SUSTAINED_MIN_S


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"duration_s": 2.0}, "at least"),
        ({"duration_s": 40.0, "window_s": 30.0}, "overlap"),
        ({"cameras": 0}, "between 1 and 16"),
        ({"cameras": 17}, "between 1 and 16"),
        ({"source": "webcam", "cameras": 2}, "exactly one camera"),
        ({"source_fps": 0.0}, "positive"),
    ],
)
def test_invalid_specs_are_rejected_with_a_reason(kw: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError) as e:
        spec(**kw).validate()
    assert any(message in issue for issue in e.value.issues)


def test_a_short_run_is_not_called_sustained() -> None:
    assert not spec(duration_s=60.0).sustained


# ------------------------------------------------------------------ report


def fake_result(*, sustained: bool = True, gpu: bool = True) -> BenchResult:
    duration = 120.0 if sustained else 20.0
    pts = [
        point(
            float(t),
            gpu_util=50.0 if gpu else None,
            sm=1500.0 if gpu else None,
            temp=60.0 if gpu else None,
        )
        for t in range(1, int(duration) + 1)
    ]
    first, last = first_and_last(pts, duration, 10.0)
    return BenchResult(
        spec=spec(name="gpu-test", duration_s=duration, window_s=10.0),
        environment={
            "vigil": "0.1.0",
            "git_commit": "abc1234",
            "git_dirty": False,
            "onnxruntime": "1.26.0",
            "onnxruntime_build": "gpu",
            "cpu": "Test CPU",
            "cpu_physical": 4,
            "cpu_logical": 8,
            "ram_gb": 16.0,
            "platform": "Windows",
            "gpu": {
                "name": "Test GPU",
                "vram_total_mb": 6144,
                "power_limit_w": 80.0,
                "driver": "1.0",
            }
            if gpu
            else None,
        },
        started_utc="2026-10-02T10:00:00+00:00",
        duration_s=duration,
        load_s=1.5,
        model={
            "name": "yolox_s",
            "version": "1",
            "backend": "onnxruntime",
            "input_size_px": 640,
            "precision": "fp32+tf32",
            "device": "cuda:0 Test GPU",
            "license": "Apache-2.0",
            "weights_sha256": "0" * 64,
        },
        vram_baseline_mb=900.0 if gpu else None,
        points=tuple(pts),
        whole=summarise(pts),
        first=first,
        last=last,
        stream_frames_delivered=500,
        health_issues=(),
        notes=("a note",),
    )


def test_a_sustained_result_renders_every_requested_measurement() -> None:
    md = render_markdown([result_to_dict(fake_result())])
    for needle in (
        "Sustained benchmarks",
        "FPS, mean",
        "FPS, median",
        "inference latency",
        "capture to annotated JPEG",
        "frames dropped",
        "system CPU",
        "GPU utilisation",
        "VRAM in use",
        "GPU SM clock",
        "first 10 s",
        "last 10 s",
        "yolox_s",
        "commit `abc1234`",
        "Test GPU",
    ):
        assert needle in md, needle


def test_missing_gpu_data_renders_as_na_never_zero() -> None:
    md = render_markdown([result_to_dict(fake_result(gpu=False))])
    gpu_lines = [ln for ln in md.splitlines() if "GPU utilisation" in ln or "SM clock" in ln]
    assert gpu_lines and all("n/a" in ln for ln in gpu_lines)
    assert "no NVIDIA GPU measured" in md


def test_short_runs_are_screening_runs_and_never_listed_as_sustained() -> None:
    md = render_markdown([result_to_dict(fake_result(sustained=False))])
    assert "_None recorded yet._" in md and "Screening runs" in md


def test_a_report_with_no_results_says_so() -> None:
    assert "_None recorded yet._" in render_markdown([])


def test_the_report_states_that_nothing_is_estimated() -> None:
    assert "nothing is estimated" in render_markdown([]).lower()


def test_results_round_trip_through_disk(tmp_path: Path) -> None:
    r = fake_result()
    path = save_result(r, tmp_path)
    assert path.name.endswith("-gpu-test.json")
    (loaded,) = load_results(tmp_path)
    assert loaded["spec"]["name"] == "gpu-test" and len(loaded["per_second"]) == 120
    assert loaded["whole"]["fps_mean"] == pytest.approx(30.0)


def test_write_report_generates_the_file(tmp_path: Path) -> None:
    save_result(fake_result(), tmp_path / "results")
    out = tmp_path / "docs" / "PERFORMANCE.md"
    assert write_report(tmp_path / "results", out) == 1
    assert out.read_text(encoding="utf-8").startswith("# Performance")


def test_unreadable_or_unknown_results_are_errors(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ReportError, match="cannot read"):
        load_results(tmp_path)
    (tmp_path / "bad.json").write_text(json.dumps({"format": 99}), encoding="utf-8")
    with pytest.raises(ReportError, match="unknown format"):
        load_results(tmp_path)


# ------------------------------------------------------------------ a real (short) run


def prepared_config(tmp_path: Path) -> Path:
    """A config dir whose model store holds a generated model with one planted detection."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    write_model(
        tmp_path / "var" / "models",
        name="fake_yolox.onnx",
        size=416,
        boxes=[PlantedBox(200, 150, 100, 80, 0, 0.9)],
    )
    return config_dir


def test_a_real_short_benchmark_measures_the_actual_pipeline(tmp_path: Path) -> None:
    result = run_benchmark(
        BenchSpec(
            name="short",
            detector="fake_yolox",
            device="cpu",
            source="synthetic",
            width_px=320,
            height_px=240,
            source_fps=20.0,
            infer_fps=20.0,
            duration_s=6.0,
            window_s=2.0,
            stream=True,
        ),
        config_dir=prepared_config(tmp_path),
    )
    assert 5.5 <= result.duration_s <= 8.0 and len(result.points) >= 5
    assert not result.sustained  # 6 s is a screening run, and says so
    w = result.whole
    assert w.inferred > 60 and w.fps_mean is not None and 14.0 <= w.fps_mean <= 22.0  # ~20 fps
    assert w.dropped == 0
    assert w.infer_ms.count == w.inferred and w.infer_ms.p50 is not None
    assert set(w.stage_ms) == {"preprocess", "infer", "postprocess"}
    assert w.end_to_end_ms.count > 0  # a stream viewer was attached, so e2e was measured
    assert result.model["device"] == "cpu" and result.model["backend"] == "onnxruntime"
    assert result.stream_frames_delivered > 0
    assert result.first.seconds <= 2.5 and result.last.seconds <= 2.5
    assert result.environment["onnxruntime"] and result.environment["python"]


def test_a_benchmark_aborts_loudly_when_the_system_cannot_work(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()  # no model in the store
    with pytest.raises(BenchError, match="failed to start"):
        run_benchmark(
            BenchSpec(
                name="x",
                detector="fake_yolox",
                device="cpu",
                source="synthetic",
                duration_s=6.0,
                window_s=2.0,
            ),
            config_dir=config_dir,
        )


def test_a_benchmark_never_silently_measures_the_wrong_device(tmp_path: Path) -> None:
    """Asking for CUDA on a build without it must fail, not quietly benchmark the CPU."""
    from vigil.vision.device import CUDA_PROVIDER, available_providers

    if CUDA_PROVIDER in available_providers():
        pytest.skip("CUDA is available here, so there is no missing-device case to provoke")
    with pytest.raises(BenchError):
        run_benchmark(
            BenchSpec(
                name="x",
                detector="fake_yolox",
                device="cuda",
                source="synthetic",
                duration_s=6.0,
                window_s=2.0,
            ),
            config_dir=prepared_config(tmp_path),
        )
