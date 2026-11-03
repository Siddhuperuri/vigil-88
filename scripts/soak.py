"""A soak run of the engine as it ships: two synthetic cameras, sampled while it runs.

    python scripts/soak.py <engine exe> <minutes> <out.csv>

It starts the engine the way the desktop shell does (`serve`, stopped by closing its input),
samples its memory, handles, threads and CPU, and asks it over HTTP whether it still answers. At the
end it compares the start of the run with the end and prints a verdict that says how long it ran:
a short soak does not show that a long one would pass.

Only the engine is soaked. The Electron shell and the console are not, and no detection model is
loaded, so the figures say nothing about GPU or model memory.
"""

from __future__ import annotations

import csv
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import psutil

SAMPLE_S = 15
WARMUP_S = 120  # model and cache start-up is not growth
CAMERAS = 2
CONFIG = """version: 1
paths:
  data_dir: ./var
cameras:
  - $include: cameras/*.yaml
logging:
  level: ERROR
"""
CAMERA = """camera_id: soak{n}
source:
  kind: synthetic
  fps: 15
target_inference_fps: 5
"""


def start(exe: str, root: Path) -> tuple[subprocess.Popen[str], str]:
    config = root / "config"
    (config / "cameras").mkdir(parents=True)
    (config / "vigil.yaml").write_text(CONFIG, encoding="utf-8")
    for n in range(CAMERAS):
        (config / "cameras" / f"soak{n}.yaml").write_text(CAMERA.format(n=n), encoding="utf-8")
    engine = subprocess.Popen(  # noqa: S603 - the executable is named by the person running this
        [exe, "serve", "--port", "0", "--stop-on-stdin-eof", "--config-dir", str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=root,
    )
    assert engine.stdout is not None
    for line in engine.stdout:
        event = json.loads(line)
        if event.get("event") == "ready":
            # Keep draining its output so a full pipe can never stall the engine being measured.
            threading.Thread(
                target=lambda: [None for _ in engine.stdout or []], daemon=True
            ).start()
            return engine, str(event["url"])
        if event.get("event") == "failed":
            raise SystemExit(f"the engine failed to start: {event.get('message')}")
    raise SystemExit("the engine exited before it was ready")


def answer_ms(url: str) -> float | None:
    began = time.perf_counter()
    try:
        with urllib.request.urlopen(f"{url}/api/v1/auth/status", timeout=5) as reply:  # noqa: S310
            reply.read()
    except OSError:
        return None
    return (time.perf_counter() - began) * 1000


def verdict(rows: list[dict[str, float]], minutes: float) -> list[str]:
    settled = [r for r in rows if r["t_s"] >= WARMUP_S]
    if len(settled) < 8:
        return [f"Too short to judge: {minutes:g} min leaves too few samples after the warm-up."]
    edge = max(4, len(settled) // 5)
    first, last = settled[:edge], settled[-edge:]
    mean = statistics.fmean
    rss_a, rss_b = mean(r["rss_mb"] for r in first), mean(r["rss_mb"] for r in last)
    out = [
        f"Ran {minutes:g} min (the plan asks for 480). Samples every {SAMPLE_S} s.",
        f"RSS: {rss_a:.0f} MB at the start of the settled period, {rss_b:.0f} MB at the end "
        f"({(rss_b - rss_a) / rss_a * 100:+.1f}%); peak {max(r['rss_mb'] for r in rows):.0f} MB.",
        f"Handles: {min(r['handles'] for r in rows):.0f} to {max(r['handles'] for r in rows):.0f}. "
        f"Threads: {min(r['threads'] for r in rows):.0f} to {max(r['threads'] for r in rows):.0f}.",
        f"CPU: mean {mean(r['cpu'] for r in rows):.1f}% of one core.",
    ]
    missed = sum(1 for r in rows if r["answer_ms"] < 0)
    answered = [r["answer_ms"] for r in rows if r["answer_ms"] >= 0]
    if answered:
        out.append(
            f"HTTP answer: median {statistics.median(answered):.0f} ms, "
            f"worst {max(answered):.0f} ms, "
            f"{missed} of {len(rows)} samples unanswered."
        )
    bounded = rss_b <= rss_a * 1.10 and missed == 0
    out.append(
        "Verdict on this run: memory and handles stayed bounded."
        if bounded
        else "Verdict on this run: NOT shown to be bounded; read the figures above."
    )
    return out


def main(exe: str, minutes: float, out: str) -> None:
    with tempfile.TemporaryDirectory(prefix="vigil-soak-") as tmp:
        engine, url = start(exe, Path(tmp))
        proc = psutil.Process(engine.pid)
        proc.cpu_percent(None)
        rows: list[dict[str, float]] = []
        began = time.time()
        while time.time() - began < minutes * 60:
            time.sleep(SAMPLE_S)
            if engine.poll() is not None:
                raise SystemExit(f"the engine died after {time.time() - began:.0f} s")
            reply = answer_ms(url)
            rows.append(
                {
                    "t_s": round(time.time() - began),
                    "rss_mb": proc.memory_info().rss / 1_048_576,
                    "handles": float(proc.num_handles()),
                    "threads": float(proc.num_threads()),
                    "cpu": proc.cpu_percent(None),
                    "answer_ms": -1.0 if reply is None else reply,
                }
            )
        stopped = time.perf_counter()
        assert engine.stdin is not None
        engine.stdin.close()  # the shell's own shutdown request
        try:
            engine.wait(timeout=30)
            clean = f"stopped on request in {(time.perf_counter() - stopped) * 1000:.0f} ms"
        except subprocess.TimeoutExpired:
            engine.kill()
            clean = "did NOT stop within 30 s and was killed"
    with Path(out).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for line in [*verdict(rows, minutes), f"Shutdown: {clean}."]:
        print(line)


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]), sys.argv[3])
