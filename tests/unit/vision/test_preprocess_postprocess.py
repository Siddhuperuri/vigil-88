from __future__ import annotations

import cv2
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.support.onnx_models import PlantedBox, raw_predictions
from vigil.core.errors import CapabilityError, InferenceError
from vigil.domain import ObjectClass
from vigil.observability.metrics import MetricsRegistry
from vigil.vision.labels import (
    COCO_80,
    DEFAULT_COCO_LABEL_MAP,
    UNMAPPED_METRIC,
    LabelMapper,
)
from vigil.vision.postprocess import YoloxDecoder, class_aware_nms, nms, postprocess_yolox
from vigil.vision.preprocess import PAD_VALUE, compute_letterbox, letterbox_bgr

PERSON, CAR, BOOK = COCO_80.index("person"), COCO_80.index("car"), COCO_80.index("book")


def image(width: int = 640, height: int = 480, seed: int = 0) -> np.ndarray:  # type: ignore[type-arg]
    return np.random.default_rng(seed).integers(0, 256, (height, width, 3), dtype=np.uint8)


# ------------------------------------------------------------------ letterbox


def test_letterbox_geometry_for_a_landscape_frame() -> None:
    t = compute_letterbox(640, 480, 416)
    assert t.scale == pytest.approx(0.65)
    assert (t.resized_width_px, t.resized_height_px) == (416, 312)


def test_letterbox_output_shape_dtype_and_padding() -> None:
    chw, t = letterbox_bgr(image(), 416)
    assert chw.shape == (3, 416, 416) and chw.dtype == np.float32 and chw.flags.c_contiguous
    assert (chw[:, t.resized_height_px :, :] == PAD_VALUE).all()  # bottom padding
    assert chw[:, : t.resized_height_px, :].std() > 10  # the image region is not flat


def test_letterbox_is_exactly_deterministic() -> None:
    img = image(seed=3)
    a, ta = letterbox_bgr(img, 416)
    b, tb = letterbox_bgr(img.copy(), 416)
    assert a.tobytes() == b.tobytes() and ta == tb


def test_letterbox_matches_a_direct_resize_of_the_image_region() -> None:
    img = image(seed=5)
    chw, t = letterbox_bgr(img, 640)
    direct = cv2.resize(
        img, (t.resized_width_px, t.resized_height_px), interpolation=cv2.INTER_LINEAR
    )
    region = chw[:, : t.resized_height_px, : t.resized_width_px].transpose(1, 2, 0)
    assert np.array_equal(region, direct.astype(np.float32))


def test_letterbox_keeps_bgr_channel_order_and_raw_range() -> None:
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[..., 0], img[..., 1], img[..., 2] = 10, 20, 30  # B, G, R
    chw, _ = letterbox_bgr(img, 32)
    assert chw[0, 0, 0] == 10 and chw[1, 0, 0] == 20 and chw[2, 0, 0] == 30  # not normalised


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((10, 10), np.uint8),
        np.zeros((10, 10, 4), np.uint8),
        np.zeros((10, 10, 3), np.float32),
    ],
)
def test_letterbox_rejects_unsupported_images(bad: np.ndarray) -> None:  # type: ignore[type-arg]
    with pytest.raises(InferenceError, match="expected"):
        letterbox_bgr(bad, 416)


def test_letterbox_rejects_degenerate_sizes() -> None:
    with pytest.raises(InferenceError):
        compute_letterbox(0, 480, 416)


@given(st.integers(16, 1920), st.integers(16, 1080), st.sampled_from([320, 416, 640]))
def test_letterbox_inverse_maps_model_corners_back_into_the_frame(
    w: int, h: int, size: int
) -> None:
    t = compute_letterbox(w, h, size)
    box = np.array([[0.0, 0.0, t.resized_width_px, t.resized_height_px]], dtype=np.float32)
    x1, y1, x2, y2 = t.to_source_xyxy(box)[0]
    assert x1 == 0 and y1 == 0
    assert abs(x2 - w) <= 1.5 / t.scale and abs(y2 - h) <= 1.5 / t.scale  # int() truncation
    assert 0 <= x2 <= w and 0 <= y2 <= h


def test_to_source_clips_to_the_frame() -> None:
    t = compute_letterbox(640, 480, 416)
    out = t.to_source_xyxy(np.array([[-50.0, -50.0, 900.0, 900.0]], dtype=np.float32))
    assert out.tolist() == [[0.0, 0.0, 640.0, 480.0]]


# ------------------------------------------------------------------ NMS


def boxes_of(*b: tuple[float, float, float, float]) -> np.ndarray:  # type: ignore[type-arg]
    return np.array(b, dtype=np.float32)


def test_nms_suppresses_overlapping_lower_scores_and_keeps_distinct_boxes() -> None:
    boxes = boxes_of((0, 0, 100, 100), (5, 5, 105, 105), (300, 300, 400, 400))
    keep = nms(boxes, np.array([0.9, 0.8, 0.7], dtype=np.float32), 0.45)
    assert keep.tolist() == [0, 2]


def test_nms_is_stable_for_equal_scores() -> None:
    boxes = boxes_of((0, 0, 100, 100), (2, 2, 102, 102))
    keep = nms(boxes, np.array([0.5, 0.5], dtype=np.float32), 0.45)
    assert keep.tolist() == [0]  # the earlier box wins, every time


def test_nms_of_nothing_is_nothing() -> None:
    assert nms(np.empty((0, 4), np.float32), np.empty(0, np.float32), 0.5).size == 0


def test_class_aware_nms_never_suppresses_across_classes() -> None:
    boxes = boxes_of((0, 0, 100, 100), (4, 4, 104, 104))
    scores = np.array([0.9, 0.8], dtype=np.float32)
    same = class_aware_nms(boxes, scores, np.array([0, 0]), 0.45)
    diff = class_aware_nms(boxes, scores, np.array([0, 2]), 0.45)
    assert same.tolist() == [0] and sorted(diff.tolist()) == [0, 1]


# ------------------------------------------------------------------ decode + postprocess


def run(
    boxes: list[PlantedBox],
    frame: tuple[int, int] = (640, 480),
    size: int = 416,
    *,
    metrics: MetricsRegistry | None = None,
    score: float = 0.25,
    max_det: int = 300,
):
    decoder = YoloxDecoder(size, 80)
    t = compute_letterbox(frame[0], frame[1], size)
    mapper = LabelMapper(COCO_80, {}, metrics)
    return postprocess_yolox(
        raw_predictions(size, boxes),
        decoder,
        t,
        mapper,
        score_threshold=score,
        iou_threshold=0.45,
        max_detections=max_det,
    )


def test_decoder_slot_counts_match_the_real_models() -> None:
    assert YoloxDecoder(416, 80).num_predictions == 3549
    assert YoloxDecoder(640, 80).num_predictions == 8400


def test_decoder_rejects_sizes_that_are_not_stride_multiples() -> None:
    with pytest.raises(InferenceError, match="multiple"):
        YoloxDecoder(100, 80)


def test_a_planted_box_is_recovered_in_source_pixels() -> None:
    (d,) = run([PlantedBox(cx=200, cy=150, w=100, h=80, class_id=PERSON, score=0.9)])
    # model space -> source space is a division by the letterbox scale (0.65)
    assert (d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2) == pytest.approx(
        (150 / 0.65, 110 / 0.65, 250 / 0.65, 190 / 0.65), abs=0.5
    )
    assert d.object_class is ObjectClass.PERSON and d.native_label == "person"
    assert d.confidence_ratio == pytest.approx(0.9, abs=1e-4)


def test_no_planted_boxes_means_no_detections_not_garbage() -> None:
    assert run([]) == []


def test_confidence_threshold_filters() -> None:
    boxes = [PlantedBox(100, 100, 60, 60, PERSON, 0.9), PlantedBox(300, 250, 60, 60, CAR, 0.2)]
    assert [d.object_class for d in run(boxes)] == [ObjectClass.PERSON]
    assert len(run(boxes, score=0.1)) == 2


def test_detections_are_sorted_by_confidence() -> None:
    boxes = [PlantedBox(100, 100, 60, 60, PERSON, 0.4), PlantedBox(300, 250, 60, 60, CAR, 0.9)]
    assert [round(d.confidence_ratio, 1) for d in run(boxes)] == [0.9, 0.4]


def test_max_detections_caps_the_result() -> None:
    boxes = [PlantedBox(40 + 80 * i, 100, 50, 50, PERSON, 0.9 - 0.01 * i) for i in range(5)]
    assert len(run(boxes, max_det=3)) == 3


def test_unmapped_classes_are_dropped_and_counted_per_label() -> None:
    metrics = MetricsRegistry()
    boxes = [PlantedBox(100, 100, 60, 60, PERSON, 0.9), PlantedBox(300, 250, 60, 60, BOOK, 0.9)]
    assert [d.object_class for d in run(boxes, metrics=metrics)] == [ObjectClass.PERSON]
    assert metrics.counter(UNMAPPED_METRIC, label="book").value == 1


def test_boxes_are_clipped_to_the_frame() -> None:
    (d,) = run([PlantedBox(cx=20, cy=20, w=100, h=100, class_id=PERSON, score=0.9)])
    assert d.bbox.x1 == 0.0 and d.bbox.y1 == 0.0  # the model box spilled over the edge


def test_nan_or_inf_output_is_an_error_never_a_detection() -> None:
    decoder = YoloxDecoder(416, 80)
    for bad in (np.nan, np.inf):
        raw = raw_predictions(416, [])
        raw[0, 100, 4] = bad
        with pytest.raises(InferenceError, match="NaN or infinite"):
            postprocess_yolox(
                raw,
                decoder,
                compute_letterbox(640, 480, 416),
                LabelMapper(COCO_80, {}),
                score_threshold=0.25,
                iou_threshold=0.45,
                max_detections=10,
            )


@pytest.mark.parametrize("shape", [(1, 100, 85), (1, 3549, 84), (3549, 85, 1), (1, 3549)])
def test_wrongly_shaped_output_is_an_error(shape: tuple[int, ...]) -> None:
    decoder = YoloxDecoder(416, 80)
    with pytest.raises(InferenceError, match="shape"):
        decoder.validate(np.zeros(shape, dtype=np.float32))


def test_the_batch_axis_is_optional_on_input() -> None:
    decoder = YoloxDecoder(416, 80)
    assert decoder.validate(np.zeros((3549, 85), dtype=np.float32)).shape == (3549, 85)


# ------------------------------------------------------------------ labels


def test_every_default_mapping_names_a_real_coco_label_and_object_class() -> None:
    assert len(COCO_80) == 80 and len(set(COCO_80)) == 80
    mapper = LabelMapper(COCO_80, {})
    assert set(DEFAULT_COCO_LABEL_MAP) <= set(COCO_80)
    assert mapper.to_canonical(PERSON) is ObjectClass.PERSON
    assert mapper.to_canonical(COCO_80.index("handbag")) is ObjectClass.OBJECT_BAG


def test_a_custom_label_map_replaces_the_default() -> None:
    mapper = LabelMapper(COCO_80, {"car": "VEHICLE_TRUCK"})
    assert mapper.to_canonical(CAR) is ObjectClass.VEHICLE_TRUCK
    assert mapper.to_canonical(PERSON) is None  # no longer mapped


def test_bad_label_maps_fail_at_construction() -> None:
    with pytest.raises(CapabilityError, match="unknown ObjectClass"):
        LabelMapper(COCO_80, {"person": "HUMAN"})
    with pytest.raises(CapabilityError, match="never emits"):
        LabelMapper(COCO_80, {"unicorn": "PERSON"})
