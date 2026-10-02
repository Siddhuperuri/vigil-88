"""Label normalisation (04 §7).

Model-native class names are a model's business, not the system's. They are mapped to the
canonical `ObjectClass` here, once, so modules reference `ObjectClass.PERSON` and never a raw
string, and swapping detectors touches one config key. Labels with no mapping are dropped
and counted per label, so a detector swap that changes the label space shows up as a metric
instead of as modules mysteriously going quiet.
"""

from __future__ import annotations

from collections.abc import Mapping

from vigil.core.errors import CapabilityError
from vigil.domain.enums import ObjectClass
from vigil.observability.metrics import MetricsRegistry

UNMAPPED_METRIC = "vigil_unmapped_labels_total"

# The 80 COCO classes in the order YOLOX emits them.
COCO_80: tuple[str, ...] = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
)

# Only the classes this system has a canonical meaning for. Everything else in COCO (chairs,
# cups, ...) is deliberately unmapped and therefore dropped and counted.
DEFAULT_COCO_LABEL_MAP: Mapping[str, str] = {
    "person": "PERSON",
    "bicycle": "CYCLE_BICYCLE",
    "car": "VEHICLE_CAR",
    "motorcycle": "VEHICLE_MOTORCYCLE",
    "bus": "VEHICLE_BUS",
    "truck": "VEHICLE_TRUCK",
    "backpack": "OBJECT_BAG",
    "handbag": "OBJECT_BAG",
    "suitcase": "OBJECT_BAG",
    "bird": "ANIMAL",
    "cat": "ANIMAL",
    "dog": "ANIMAL",
    "horse": "ANIMAL",
    "sheep": "ANIMAL",
    "cow": "ANIMAL",
    "elephant": "ANIMAL",
    "bear": "ANIMAL",
    "zebra": "ANIMAL",
    "giraffe": "ANIMAL",
}


class LabelMapper:
    def __init__(
        self,
        native_labels: tuple[str, ...],
        label_map: Mapping[str, str],
        metrics: MetricsRegistry | None = None,
    ) -> None:
        mapping = dict(label_map) if label_map else dict(DEFAULT_COCO_LABEL_MAP)
        by_name = {c.name: c for c in ObjectClass}
        bad = sorted({v for v in mapping.values() if v not in by_name})
        if bad:
            raise CapabilityError(
                f"vision.label_map names unknown ObjectClass value(s): {', '.join(bad)}",
                context={"valid": sorted(by_name)},
            )
        unknown_native = sorted(set(mapping) - set(native_labels))
        if unknown_native:
            raise CapabilityError(
                f"vision.label_map references label(s) this model never emits: "
                f"{', '.join(unknown_native)}"
            )
        self._native = native_labels
        self._by_index: tuple[ObjectClass | None, ...] = tuple(
            by_name[mapping[n]] if n in mapping else None for n in native_labels
        )
        self._metrics = metrics

    @property
    def num_classes(self) -> int:
        return len(self._native)

    def native_label(self, class_index: int) -> str:
        return self._native[class_index]

    def to_canonical(self, class_index: int) -> ObjectClass | None:
        """The canonical class, or None (and a count) if this label is not mapped."""
        mapped = self._by_index[class_index]
        if mapped is None and self._metrics is not None:
            self._metrics.counter(UNMAPPED_METRIC, label=self._native[class_index]).inc()
        return mapped
