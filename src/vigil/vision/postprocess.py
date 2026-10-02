"""YOLOX output decoding, NMS and mapping to domain Detections (04 §3.1).

The released YOLOX ONNX files emit undecoded predictions of shape (1, N, 5 + C):
  [dx, dy, log_w, log_h, objectness, class scores...]
where objectness and class scores have already been through a sigmoid, and the box terms
are offsets on the stride grid. Decoding, confidence filtering and NMS happen here, and
the returned boxes are in SOURCE-frame pixels.

A result that is not the expected shape, or that contains NaN/inf, is an `InferenceError`.
It is never turned into detections.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from vigil.core.errors import InferenceError
from vigil.core.geometry import BBox
from vigil.domain.detection import Detection
from vigil.vision.labels import LabelMapper
from vigil.vision.preprocess import F32, LetterboxTransform

STRIDES: tuple[int, ...] = (8, 16, 32)
_BOX_TERMS = 5  # dx, dy, log_w, log_h, objectness
_EPS = 1e-9
Indices = npt.NDArray[np.intp]


class YoloxDecoder:
    """Precomputed stride grids for one square input size."""

    def __init__(self, size_px: int, num_classes: int) -> None:
        if any(size_px % s for s in STRIDES):
            raise InferenceError(f"input size {size_px} is not a multiple of {STRIDES}")
        grids: list[F32] = []
        strides: list[F32] = []
        for s in STRIDES:
            n = size_px // s
            xs, ys = np.meshgrid(np.arange(n), np.arange(n))
            grid = np.stack((xs, ys), axis=2).reshape(-1, 2).astype(np.float32)
            grids.append(grid)
            strides.append(np.full((grid.shape[0], 1), s, dtype=np.float32))
        self.size_px = size_px
        self.num_classes = num_classes
        self._grids: F32 = np.concatenate(grids, axis=0)
        self._strides: F32 = np.concatenate(strides, axis=0)
        self.num_predictions = int(self._grids.shape[0])

    def validate(self, raw: F32) -> F32:
        """Squeeze the batch axis and reject anything that is not a sane prediction tensor."""
        arr = raw[0] if raw.ndim == 3 and raw.shape[0] == 1 else raw
        expected = (self.num_predictions, _BOX_TERMS + self.num_classes)
        if arr.shape != expected:
            raise InferenceError(
                f"detector output has shape {tuple(raw.shape)}, expected (1, "
                f"{expected[0]}, {expected[1]}) for a {self.size_px}px input"
            )
        if not np.isfinite(arr).all():
            raise InferenceError("detector output contains NaN or infinite values")
        return arr

    def decode(self, arr: F32) -> tuple[F32, F32, F32]:
        """-> (boxes xyxy in model space, objectness, class scores)."""
        xy = (arr[:, :2] + self._grids) * self._strides
        wh = np.exp(np.clip(arr[:, 2:4], -20.0, 20.0)) * self._strides
        half = wh / 2.0
        boxes = np.concatenate((xy - half, xy + half), axis=1).astype(np.float32, copy=False)
        return boxes, arr[:, 4], arr[:, _BOX_TERMS:]


def nms(boxes: F32, scores: F32, iou_threshold: float) -> Indices:
    """Greedy non-maximum suppression. Stable, so equal scores resolve deterministically."""
    if boxes.shape[0] == 0:
        return np.empty(0, dtype=np.intp)
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order: Indices = np.argsort(-scores, kind="stable")
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        w = np.maximum(0.0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        h = np.maximum(0.0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = w * h
        iou = inter / (areas[i] + areas[rest] - inter + _EPS)
        order = rest[iou <= iou_threshold]
    return np.asarray(keep, dtype=np.intp)


def class_aware_nms(boxes: F32, scores: F32, class_ids: Indices, iou_threshold: float) -> Indices:
    """NMS that never suppresses across classes (a person overlapping a bag keeps both)."""
    if boxes.shape[0] == 0:
        return np.empty(0, dtype=np.intp)
    span = float(boxes.max()) + 1.0
    shifted = boxes + (class_ids.astype(np.float32) * span)[:, None]
    return nms(shifted, scores, iou_threshold)


def postprocess_yolox(
    raw: F32,
    decoder: YoloxDecoder,
    transform: LetterboxTransform,
    mapper: LabelMapper,
    *,
    score_threshold: float,
    iou_threshold: float,
    max_detections: int,
) -> list[Detection]:
    arr = decoder.validate(raw)
    boxes, objectness, class_scores = decoder.decode(arr)

    scores_all = objectness[:, None] * class_scores
    class_ids: Indices = scores_all.argmax(axis=1)
    best = scores_all[np.arange(scores_all.shape[0]), class_ids]
    candidates = np.flatnonzero(best >= score_threshold)
    if candidates.size == 0:
        return []

    cand_boxes, cand_scores, cand_ids = boxes[candidates], best[candidates], class_ids[candidates]
    keep = class_aware_nms(cand_boxes, cand_scores, cand_ids, iou_threshold)[:max_detections]
    source_boxes = transform.to_source_xyxy(cand_boxes[keep])

    detections: list[Detection] = []
    for box, score, cid in zip(source_boxes, cand_scores[keep], cand_ids[keep], strict=True):
        canonical = mapper.to_canonical(int(cid))
        if canonical is None:
            continue
        x1, y1, x2, y2 = (float(v) for v in box)
        if x2 <= x1 or y2 <= y1:
            continue  # collapsed to nothing after clipping to the frame
        detections.append(
            Detection(
                bbox=BBox(x1, y1, x2, y2),
                object_class=canonical,
                confidence_ratio=float(min(max(score, 0.0), 1.0)),
                native_label=mapper.native_label(int(cid)),
            )
        )
    return detections
