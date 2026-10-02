"""Build tiny but REAL ONNX models that speak the YOLOX output contract.

The graph ignores its input's values but depends on it structurally (a zero-multiplied term is
added), so ONNX Runtime genuinely executes it, and emits a fixed, known prediction tensor of
shape (1, N, 85). That lets tests assert decode, NMS and un-letterboxing EXACTLY, with real
ORT inference and no downloaded weights.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

from vigil.vision.models import MANIFEST_NAME

STRIDES = (8, 16, 32)
NUM_CLASSES = 80
PRED_WIDTH = 5 + NUM_CLASSES


@dataclass(frozen=True)
class PlantedBox:
    """A detection to plant, in MODEL-space pixels (before un-letterboxing)."""

    cx: float
    cy: float
    w: float
    h: float
    class_id: int
    score: float


def grid_layout(size: int) -> list[tuple[int, int, int]]:
    """(stride, gx, gy) for every prediction slot, in the order YOLOX emits them."""
    out: list[tuple[int, int, int]] = []
    for s in STRIDES:
        n = size // s
        for gy in range(n):
            for gx in range(n):
                out.append((s, gx, gy))
    return out


def raw_predictions(size: int, boxes: Sequence[PlantedBox]) -> np.ndarray:  # type: ignore[type-arg]
    """An undecoded (1, N, 85) tensor whose decoded content is exactly `boxes`."""
    layout = grid_layout(size)
    raw = np.zeros((1, len(layout), PRED_WIDTH), dtype=np.float32)
    raw[0, :, 2:4] = -10.0  # tiny boxes: exp(-10) * stride, and objectness stays 0
    for b in boxes:
        stride = 8 if max(b.w, b.h) < 64 else 16 if max(b.w, b.h) < 160 else 32
        gx, gy = int(b.cx // stride), int(b.cy // stride)
        slot = next(i for i, (s, x, y) in enumerate(layout) if s == stride and x == gx and y == gy)
        raw[0, slot, 0] = b.cx / stride - gx
        raw[0, slot, 1] = b.cy / stride - gy
        raw[0, slot, 2] = float(np.log(b.w / stride))
        raw[0, slot, 3] = float(np.log(b.h / stride))
        raw[0, slot, 4] = 1.0  # objectness
        raw[0, slot, 5 + b.class_id] = b.score
    return raw


def build_model(
    size: int = 416,
    boxes: Sequence[PlantedBox] = (),
    *,
    constant_output: np.ndarray | None = None,  # type: ignore[type-arg]
) -> onnx.ModelProto:
    out = raw_predictions(size, boxes) if constant_output is None else constant_output
    x = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, size, size])
    y = helper.make_tensor_value_info("output", TensorProto.FLOAT, list(out.shape))
    zero = numpy_helper.from_array(np.zeros((1,), dtype=np.float32), "zero")
    const = numpy_helper.from_array(out.astype(np.float32), "const")
    nodes = [
        helper.make_node("ReduceMax", ["images"], ["peak"], keepdims=0),  # real compute on input
        helper.make_node("Mul", ["peak", "zero"], ["nothing"]),
        helper.make_node("Add", ["const", "nothing"], ["output"]),
    ]
    graph = helper.make_graph(nodes, "fake_yolox", [x], [y], initializer=[zero, const])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    return model


def write_model(
    models_dir: Path,
    name: str = "fake_yolox.onnx",
    size: int = 416,
    boxes: Sequence[PlantedBox] = (),
    *,
    constant_output: np.ndarray | None = None,  # type: ignore[type-arg]
    register: bool = True,
) -> Path:
    """Write the model and (by default) record it in MANIFEST.json."""
    models_dir.mkdir(parents=True, exist_ok=True)
    path = models_dir / name
    path.write_bytes(build_model(size, boxes, constant_output=constant_output).SerializeToString())
    if register:
        record(models_dir, path)
    return path


def record(models_dir: Path, path: Path) -> None:
    manifest_file = models_dir / MANIFEST_NAME
    models = json.loads(manifest_file.read_text())["models"] if manifest_file.exists() else {}
    data = path.read_bytes()
    models[path.name] = {
        "file": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "source": "generated for tests",
        "license": "CC0-1.0",
        "recorded_utc": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
    }
    manifest_file.write_text(json.dumps({"version": 1, "models": models}))
