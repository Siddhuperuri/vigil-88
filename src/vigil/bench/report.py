"""Benchmark results on disk, and the PERFORMANCE.md generated from them.

PERFORMANCE.md is written only by `vigil bench report` from the stored JSON results, so it
cannot contain a number nobody measured. Each run's per-second series is kept in its JSON file,
which is what makes any figure in the report checkable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from vigil.bench.runner import BenchResult
from vigil.bench.spec import SUSTAINED_MIN_S
from vigil.bench.stats import change_percent
from vigil.core.errors import VigilError

Json = Mapping[str, object]
MISSING = "n/a"


class ReportError(VigilError):
    """A stored result could not be read."""


# ------------------------------------------------------------------ storage


def result_to_dict(result: BenchResult) -> dict[str, object]:
    return {
        "format": 1,
        "sustained": result.sustained,
        "spec": asdict(result.spec),
        "environment": dict(result.environment),
        "started_utc": result.started_utc,
        "duration_s": result.duration_s,
        "load_s": result.load_s,
        "model": dict(result.model),
        "vram_baseline_mb": result.vram_baseline_mb,
        "whole": asdict(result.whole),
        "first": asdict(result.first),
        "last": asdict(result.last),
        "stream_frames_delivered": result.stream_frames_delivered,
        "health_issues": list(result.health_issues),
        "notes": list(result.notes),
        "per_second": [asdict(p) for p in result.points],
    }


def save_result(result: BenchResult, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = result.started_utc.replace(":", "").replace("-", "").split(".")[0]
    path = directory / f"{stamp}-{result.spec.name}.json"
    path.write_text(json.dumps(result_to_dict(result), indent=1) + "\n", encoding="utf-8")
    return path


def load_results(directory: Path) -> list[Json]:
    out: list[Json] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReportError(f"cannot read benchmark result {path.name}: {exc}") from exc
        if data.get("format") != 1:
            raise ReportError(f"{path.name} has an unknown format {data.get('format')!r}")
        out.append(data)
    return out


# ------------------------------------------------------------------ formatting


def _f(value: object, digits: int = 1) -> str:
    return MISSING if value is None else f"{float(value):.{digits}f}"  # type: ignore[arg-type]


def _get(d: Json, *path: str) -> object:
    cur: object = d
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def _triple(d: Json, *path: str) -> str:
    """p50 / p95 / p99 of a distribution."""
    return " / ".join(_f(_get(d, *path, k), 1) for k in ("p50", "p95", "p99"))


def _pair(d: Json, *path: str) -> str:
    return " / ".join(_f(_get(d, *path, k), 1) for k in ("p50", "p95"))


def _range(d: Json, *path: str, digits: int = 1) -> str:
    return " / ".join(_f(_get(d, *path, k), digits) for k in ("mean", "minimum", "maximum"))


def _delta(first: object, last: object) -> str:
    pct = change_percent(first, last)  # type: ignore[arg-type]
    return MISSING if pct is None else f"{pct:+.1f}%"


def _row(label: str, cells: Sequence[str]) -> str:
    return "| " + " | ".join([label, *cells]) + " |"


# ------------------------------------------------------------------ one run


def _power_text(ac: object) -> str:
    if ac is True:
        return "on AC power"
    if ac is False:
        return "**on battery**: GPU power limits and clocks are lower than on AC"
    return "no battery reported"


def _spec_table(r: Json) -> list[str]:
    s, env, model = r["spec"], r["environment"], r["model"]
    assert isinstance(s, Mapping)
    assert isinstance(env, Mapping)
    assert isinstance(model, Mapping)
    gpu = env.get("gpu")
    gpu_text = (
        f"{gpu['name']}, {gpu['vram_total_mb']} MB, driver {gpu['driver']}, "
        f"power limit {_f(gpu['power_limit_w'], 0)} W"
        if gpu
        else "no NVIDIA GPU measured"
    )
    source = (
        f"{s['cameras']} x synthetic {s['width_px']}x{s['height_px']} @ {s['source_fps']:g} fps"
        if s["source"] == "synthetic"
        else f"webcam requested {s['width_px']}x{s['height_px']} @ {s['source_fps']:g} fps"
    )
    return [
        "| | |",
        "|---|---|",
        f"| model | `{model['name']}` {model['input_size_px']}px, {model['precision']}, "
        f"{model['license']} |",
        f"| device | {model['device']} |",
        f"| source | {source} |",
        f"| target inference rate | {s['infer_fps']:g} fps per camera |",
        f"| annotated stream consumed | {'yes (in-process viewer)' if s['stream'] else 'no'} |",
        f"| duration | {_f(r['duration_s'], 1)} s measured (requested {s['duration_s']:g} s); "
        f"model load + warm-up {_f(r['load_s'], 2)} s |",
        f"| started | {r['started_utc']} |",
        f"| code | commit `{env.get('git_commit', 'unknown')}`"
        f"{' (uncommitted changes)' if env.get('git_dirty') else ''}, "
        f"onnxruntime {env.get('onnxruntime')} ({env.get('onnxruntime_build')} build) |",
        f"| GPU | {gpu_text} |",
        f"| host | {env.get('cpu')}, {env.get('cpu_physical')} cores / "
        f"{env.get('cpu_logical')} threads, {env.get('ram_gb')} GB RAM, {env.get('platform')} |",
    ]


def _comparison_table(r: Json) -> list[str]:
    s = r["spec"]
    assert isinstance(s, Mapping)
    w = f"{s['window_s']:g} s"
    head = [
        _row("metric", ["whole run", f"first {w}", f"last {w}", "change"]),
        "|---|---|---|---|---|",
    ]
    whole, first, last = r["whole"], r["first"], r["last"]
    assert isinstance(whole, Mapping)
    assert isinstance(first, Mapping)
    assert isinstance(last, Mapping)

    def three(label: str, *path: str, digits: int = 1, change: bool = True) -> str:
        a, b, c = (_get(x, *path) for x in (whole, first, last))
        return _row(
            label, [_f(a, digits), _f(b, digits), _f(c, digits), _delta(b, c) if change else ""]
        )

    def dist(label: str, *path: str) -> str:
        cells = [_triple(x, *path) for x in (whole, first, last)]
        return _row(label, [*cells, _delta(_get(first, *path, "p50"), _get(last, *path, "p50"))])

    def rng(label: str, *path: str, digits: int = 1) -> str:
        cells = [_range(x, *path, digits=digits) for x in (whole, first, last)]
        return _row(label, [*cells, _delta(_get(first, *path, "mean"), _get(last, *path, "mean"))])

    def pair(label: str, *path: str) -> str:
        cells = [_pair(x, *path) for x in (whole, first, last)]
        return _row(label, [*cells, _delta(_get(first, *path, "p50"), _get(last, *path, "p50"))])

    def count(label: str, key: str) -> str:
        return _row(label, [str(_get(x, key)) for x in (whole, first, last)] + [""])

    rows = [
        three("FPS, mean (frames inferred / second)", "fps_mean"),
        three("FPS, median of per-second rates", "fps_median"),
        three("FPS, slowest 10% of seconds", "fps_low"),
        dist("inference latency, ms (p50 / p95 / p99)", "infer_ms"),
        dist("  preprocess, ms", "stage_ms", "preprocess"),
        dist("  model run, ms", "stage_ms", "infer"),
        dist("  postprocess, ms", "stage_ms", "postprocess"),
        pair("capture to detections, ms (p50 / p95)", "capture_to_result_ms"),
        pair("capture to annotated JPEG, ms (p50 / p95)", "end_to_end_ms"),
        count("frames received", "received"),
        count("frames inferred", "inferred"),
        count("frames skipped (not analysed: normal)", "skipped"),
        count("frames dropped (lost to a full queue: a problem)", "dropped"),
        rng("system CPU %, mean / min / max", "cpu_percent", digits=0),
        rng("process CPU %, mean / min / max (100 = one core)", "process_cpu_percent", digits=0),
        rng("process RAM, MB, mean / min / max", "rss_mb", digits=0),
        rng("GPU utilisation %, mean / min / max", "gpu_util_percent", digits=0),
        rng("VRAM in use, MB, mean / min / max (whole GPU)", "gpu_used_mb", digits=0),
        rng("GPU temperature C, mean / min / max", "gpu_temp_c", digits=0),
        rng("GPU SM clock MHz, mean / min / max", "gpu_sm_clock_mhz", digits=0),
        rng("GPU power W, mean / min / max", "gpu_power_w", digits=1),
        three(
            "seconds the GPU reported being throttled", "throttled_seconds", digits=0, change=False
        ),
    ]
    return [*head, *rows]


def _run_section(r: Json) -> list[str]:
    s = r["spec"]
    assert isinstance(s, Mapping)
    baseline = r.get("vram_baseline_mb")
    peak = _get(r, "whole", "gpu_used_mb", "maximum")
    lines = [f"### {s['name']}", "", *_spec_table(r), "", *_comparison_table(r), ""]
    if baseline is not None and peak is not None:
        lines.append(
            f"VRAM: {_f(baseline, 0)} MB was already in use before the run (desktop and other "
            f"programs); the peak during the run was {_f(peak, 0)} MB, so this workload added "
            f"about {_f(float(peak) - float(baseline), 0)} MB.  "  # type: ignore[arg-type]
        )
    reasons = _get(r, "whole", "throttle_reasons")
    lines.append(
        f"GPU throttle reasons seen: {', '.join(reasons) if reasons else 'none'}.  "  # type: ignore[arg-type]
    )
    if r.get("stream_frames_delivered"):
        lines.append(
            f"Annotated frames delivered to the in-process viewer: "
            f"{r['stream_frames_delivered']}.  "
        )
    issues = r.get("health_issues")
    if issues:
        lines.append(f"Health issues at the end of the run: {'; '.join(issues)}.  ")  # type: ignore[arg-type]
    lines.append("")
    for note in r.get("notes", []):  # type: ignore[attr-defined]
        lines.append(f"- {note}")
    lines.append("")
    return lines


# ------------------------------------------------------------------ the document


def _at_a_glance(runs: Sequence[Json]) -> list[str]:
    head = _row(
        "run",
        [
            "device",
            "FPS mean (median)",
            "inference p50 / p95 ms",
            "capture to detections p50 / p95 ms",
            "skipped / dropped",
            "GPU util max %",
            "temp max C",
            "SM clock mean MHz",
            "throttled s",
        ],
    )
    rows = [
        "## At a glance",
        "",
        "Sustained runs side by side. Details for each follow.",
        "",
        head,
        "|" + "---|" * 10,
    ]
    for r in runs:
        s, m = r["spec"], r["model"]
        assert isinstance(s, Mapping)
        assert isinstance(m, Mapping)
        device = "CPU" if str(m["device"]).startswith("cpu") else "GPU"
        rows.append(
            _row(
                f"`{s['name']}`",
                [
                    device,
                    f"{_f(_get(r, 'whole', 'fps_mean'))} ({_f(_get(r, 'whole', 'fps_median'))})",
                    _pair(r, "whole", "infer_ms"),
                    _pair(r, "whole", "capture_to_result_ms"),
                    f"{_get(r, 'whole', 'skipped')} / {_get(r, 'whole', 'dropped')}",
                    _f(_get(r, "whole", "gpu_util_percent", "maximum"), 0),
                    _f(_get(r, "whole", "gpu_temp_c", "maximum"), 0),
                    _f(_get(r, "whole", "gpu_sm_clock_mhz", "mean"), 0),
                    str(_get(r, "whole", "throttled_seconds")),
                ],
            )
        )
    return [*rows, ""]


def render_markdown(results: Sequence[Json]) -> str:
    sustained = [r for r in results if r.get("sustained")]
    screening = [r for r in results if not r.get("sustained")]
    out = [
        "# Performance",
        "",
        "Generated by `vigil bench report` from the JSON results in `docs/bench-results/`. "
        "Do not edit by hand. **Every number here was measured**; nothing is estimated or "
        "extrapolated, and a quantity that could not be measured is shown as `n/a`, never `0`.",
        "",
        "## How to read this",
        "",
        "- A **sustained benchmark** runs at least "
        f"{SUSTAINED_MIN_S:g} s. Shorter runs are listed separately as *screening runs* and make "
        "no claim about sustained behaviour.",
        "- The target machine has a **laptop** GPU. Mobile GPUs change clocks with load, heat and "
        "power, so each run reports the first and the last window side by side. The "
        "*change* column is last relative to first.",
        "- **Skipped** frames were not analysed because inference is paced below the camera's "
        "frame rate (by design). **Dropped** frames were lost to a full queue and should be 0.",
        "- *Inference latency* is the detector's own time per frame. *Capture to detections* "
        "adds queueing. *Capture to annotated JPEG* also includes waiting for the next stream "
        "tick (`api.stream_fps`, default 10, so up to 100 ms), so it reflects the stream "
        "cadence as much as the cost of drawing and encoding.",
        "- A laptop GPU runs slower at a **low duty cycle**: its clocks drop between sparse "
        "frames. Compare the *model run* and *SM clock* rows across runs with different "
        "inference rates before assuming a model's speed is a constant.",
        "- **Process CPU near 100% means one core is saturated.** The pipeline's Python-side "
        "work (pre/post-processing, scheduling, encoding) then limits throughput before the "
        "GPU does; compare it with GPU utilisation.",
        "- Sources are synthetic test patterns unless stated, so post-processing cost reflects an "
        "empty scene. Latency on a real scene is covered by the webcam runs.",
        "",
    ]
    if sustained:
        out += _at_a_glance(sustained)
        out += ["## Sustained benchmarks", ""]
        for r in sustained:
            out += _run_section(r)
    else:
        out += ["## Sustained benchmarks", "", "_None recorded yet._", ""]
    if screening:
        out += [
            "## Screening runs (shorter than a sustained benchmark)",
            "",
            "These pick candidates and make no sustained-load claim.",
            "",
            _row(
                "run",
                ["model", "device", "duration s", "FPS mean", "infer p50 / p95 ms", "dropped"],
            ),
            "|---|---|---|---|---|---|",
        ]
        for r in screening:
            s, m = r["spec"], r["model"]
            assert isinstance(s, Mapping)
            assert isinstance(m, Mapping)
            out.append(
                _row(
                    str(s["name"]),
                    [
                        f"{m['name']} {m['input_size_px']}px",
                        str(m["device"]),
                        _f(r["duration_s"], 0),
                        _f(_get(r, "whole", "fps_mean"), 1),
                        _pair(r, "whole", "infer_ms"),
                        str(_get(r, "whole", "dropped")),
                    ],
                )
            )
        out.append("")
    return "\n".join(out)


def write_report(results_dir: Path, output: Path) -> int:
    results = load_results(results_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(results), encoding="utf-8")
    return len(results)
