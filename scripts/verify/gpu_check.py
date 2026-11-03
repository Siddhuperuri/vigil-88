"""Checks, on a real NVIDIA GPU, that the engine really runs on it and gets the same answers.

    python scripts/verify/gpu_check.py --dry-run               # what is here; no GPU work
    python scripts/verify/gpu_check.py <model.onnx> [--runs 50] [--out evidence.json]

`--dry-run` reads the environment only: the driver and card as `nvidia-smi` reports them, and which
execution providers the installed onnxruntime offers. It runs nothing on the card.

The full run opens the model twice, once on CUDA and once on the CPU, and reports:

  * the provider the CUDA session actually uses. onnxruntime silently falls back to the CPU when
    CUDA cannot load, so "it ran" proves nothing: the provider has to be CUDA;
  * latency over `--runs` inferences on each, and the speed-up;
  * the largest difference between the two outputs on the same input;
  * the card's memory in use before and after.

It does not say the GPU build is "supported": it says what it measured, on this card and
driver.
"""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

TOLERANCE = 1e-2  # fp32 CUDA and CPU kernels differ in the last digits, not in the answer


def smi() -> dict[str, str] | None:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    query = "name,driver_version,memory.total,memory.used,temperature.gpu"
    try:
        run = subprocess.run(  # noqa: S603 - fixed program and arguments
            [exe, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = run.stdout.strip().splitlines()[0] if run.stdout.strip() else ""
    parts = [p.strip() for p in first.split(",")]
    keys = ["name", "driver", "memory_total_mb", "memory_used_mb", "temperature_c"]
    return dict(zip(keys, parts, strict=False)) if len(parts) == len(keys) else None


def environment() -> dict[str, Any]:
    try:
        import onnxruntime as ort

        providers = list(ort.get_available_providers())
        version = ort.__version__
    except ImportError:
        providers, version = [], None
    card = smi()
    return {
        "card": card,
        "onnxruntime": version,
        "providers": providers,
        "cuda_provider_offered": "CUDAExecutionProvider" in providers,
        "verdict": (
            "A card is present and onnxruntime offers CUDA."
            if card is not None and "CUDAExecutionProvider" in providers
            else "No usable GPU path here: "
            + ("no NVIDIA driver" if card is None else "onnxruntime does not offer CUDA")
            + "."
        ),
    }


def compare(cuda: np.ndarray, cpu: np.ndarray) -> float:
    return float(np.max(np.abs(cuda.astype(np.float64) - cpu.astype(np.float64))))


def timed(session: Any, name: str, data: np.ndarray, runs: int) -> tuple[list[float], np.ndarray]:
    session.run(None, {name: data})  # warm-up
    times: list[float] = []
    out = None
    for _ in range(runs):
        began = time.perf_counter()
        out = session.run(None, {name: data})[0]
        times.append((time.perf_counter() - began) * 1000)
    return times, np.asarray(out)


def full(model: Path, runs: int) -> dict[str, Any]:
    import onnxruntime as ort

    before = smi()
    cuda = ort.InferenceSession(
        str(model), providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
    )
    cpu = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    used = cuda.get_providers()[0]
    meta = cuda.get_inputs()[0]
    shape = [d if isinstance(d, int) else 1 for d in meta.shape]
    data = np.random.default_rng(0).random(shape, dtype=np.float32)
    cuda_ms, cuda_out = timed(cuda, meta.name, data, runs)
    cpu_ms, cpu_out = timed(cpu, meta.name, data, max(3, runs // 10))
    delta = compare(cuda_out, cpu_out)
    after = smi()
    on_cuda = used == "CUDAExecutionProvider"
    return {
        "provider_used": used,
        "on_cuda": on_cuda,
        "cuda_ms_median": round(statistics.median(cuda_ms), 2),
        "cuda_ms_p95": round(sorted(cuda_ms)[int(len(cuda_ms) * 0.95) - 1], 2),
        "cpu_ms_median": round(statistics.median(cpu_ms), 2),
        "max_abs_difference": delta,
        "agrees_with_cpu": delta <= TOLERANCE,
        "memory_used_mb": {
            "before": None if before is None else before["memory_used_mb"],
            "after": None if after is None else after["memory_used_mb"],
        },
        "verdict": (
            "Ran on CUDA and agrees with the CPU."
            if on_cuda and delta <= TOLERANCE
            else "NOT VERIFIED: "
            + ("it fell back to the CPU; " if not on_cuda else "")
            + (f"outputs differ by {delta:.4g}." if delta > TOLERANCE else "")
        ),
    }


def main(argv: list[str]) -> int:
    env = environment()
    result: dict[str, Any] = {"environment": env}
    if "--dry-run" in argv or not argv:
        print(json.dumps(result, indent=2))
        return 0 if env["cuda_provider_offered"] else 1
    if not env["cuda_provider_offered"]:
        print(json.dumps(result, indent=2))
        return 1
    runs = int(argv[argv.index("--runs") + 1]) if "--runs" in argv else 50
    result["run"] = full(Path(argv[0]), runs)
    text = json.dumps(result, indent=2)
    print(text)
    if "--out" in argv:
        Path(argv[argv.index("--out") + 1]).write_text(text, encoding="utf-8")
    return 0 if result["run"]["on_cuda"] and result["run"]["agrees_with_cpu"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
