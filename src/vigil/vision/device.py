"""Compute-device resolution (03 §4).

One decision, made once at startup and logged with its reason: `auto` prefers CUDA and says
why when it does not get it. A CPU fallback nobody notices is a performance mystery waiting
to happen, so every fallback is recorded (`fell_back`) and surfaces in the model descriptor.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vigil.config.schema.vision import DeviceName
from vigil.core.errors import CapabilityError

CUDA_PROVIDER = "CUDAExecutionProvider"
CPU_PROVIDER = "CPUExecutionProvider"


@dataclass(frozen=True, slots=True)
class ResolvedDevice:
    kind: Literal["cuda", "cpu"]
    reason: str
    fell_back: bool  # CUDA was wanted (auto or explicit) but CPU is what we got


_cuda_dirs_added: list[str] = []


def nvidia_bin_dirs() -> list[Path]:
    """`bin` directories of the pip-installed NVIDIA CUDA/cuDNN wheels (site-packages/nvidia)."""
    try:
        import nvidia
    except ImportError:
        return []
    dirs: list[Path] = []
    for base in getattr(nvidia, "__path__", []):
        for sub in sorted(Path(base).iterdir()):
            if (sub / "bin").is_dir():
                dirs.append(sub / "bin")
    return dirs


def prepare_cuda_runtime() -> list[str]:
    """Make the pip-installed CUDA libraries loadable. Idempotent. Returns directories added.

    cuDNN loads its sub-libraries by bare name at run time (e.g. cudnn_engines_tensor_ir64_9.dll),
    so the directories must be on the DLL search path, not merely preloaded. That is a
    process-wide change to PATH, made only when a CUDA session is about to be created.
    """
    added: list[str] = []
    for d in nvidia_bin_dirs():
        text = str(d)
        if text in _cuda_dirs_added:
            continue
        os.add_dll_directory(text)
        os.environ["PATH"] = text + os.pathsep + os.environ.get("PATH", "")
        _cuda_dirs_added.append(text)
        added.append(text)
    return added


def available_providers() -> tuple[str, ...]:
    """Execution providers of the installed onnxruntime build; empty if it is not installed."""
    try:
        import onnxruntime
    except ImportError:
        return ()
    return tuple(onnxruntime.get_available_providers())


def resolve_device(
    wanted: DeviceName, *, allow_cpu_fallback: bool, providers: Sequence[str]
) -> ResolvedDevice:
    if wanted == "cpu":
        return ResolvedDevice("cpu", "vision.device is 'cpu'", fell_back=False)
    if CUDA_PROVIDER in providers:
        return ResolvedDevice(
            "cuda", f"{CUDA_PROVIDER} is available (vision.device={wanted})", False
        )

    why = (
        f"{CUDA_PROVIDER} is not available in this onnxruntime build "
        f"(providers: {', '.join(providers) or 'none; onnxruntime not installed'}). "
        "For GPU inference install the onnx-gpu extra: uv sync --extra onnx-gpu"
    )
    if wanted == "auto":
        return ResolvedDevice("cpu", f"{why}. Using CPU", fell_back=True)
    if allow_cpu_fallback:
        return ResolvedDevice(
            "cpu", f"vision.device is 'cuda' but {why}. Falling back to CPU", True
        )
    raise CapabilityError(
        f"vision.device is 'cuda' and vision.allow_cpu_fallback is false, but {why}",
        context={"providers": list(providers)},
    )
