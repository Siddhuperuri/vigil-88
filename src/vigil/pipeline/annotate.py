"""Server-side annotation and JPEG encoding (08 §4).

Boxes are drawn here, by the code that holds both the pixels and the detections made on those
exact pixels, so an overlay cannot lag or lead the video. (A browser <img> exposes neither the
identity nor the timestamp of the frame on screen, so a client-side canvas overlay driven by a
separate channel would desync by a variable 100-500 ms and look like a detection bug.)

Frames are immutable, so every annotated render draws on a private copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import cv2
import numpy as np

from vigil.core.errors import VigilError
from vigil.domain.enums import ObjectClass
from vigil.pipeline.results import AnalysedFrame

OVERLAY_TOKENS = ("boxes", "labels", "info")
STALE_AFTER_MS = 2000.0
_MS_PER_NS = 1e-6

# BGR, chosen to stay legible on both bright and dark scenes.
CLASS_COLOURS: Mapping[ObjectClass, tuple[int, int, int]] = {
    ObjectClass.PERSON: (80, 220, 60),
    ObjectClass.VEHICLE_CAR: (255, 160, 40),
    ObjectClass.VEHICLE_TRUCK: (255, 120, 40),
    ObjectClass.VEHICLE_BUS: (255, 90, 90),
    ObjectClass.VEHICLE_MOTORCYCLE: (255, 200, 90),
    ObjectClass.CYCLE_BICYCLE: (230, 230, 60),
    ObjectClass.OBJECT_BAG: (200, 90, 230),
    ObjectClass.ANIMAL: (60, 200, 255),
}
_DEFAULT_COLOUR = (200, 200, 200)
_HUD_COLOUR = (240, 240, 240)
_STALE_COLOUR = (60, 60, 255)


class RenderError(VigilError):
    """A frame could not be annotated or encoded."""


@dataclass(frozen=True, slots=True)
class OverlayOptions:
    boxes: bool = True
    labels: bool = True
    info: bool = True

    @property
    def key(self) -> str:
        """Canonical name: equal option sets share one encoded stream."""
        on = [t for t in OVERLAY_TOKENS if getattr(self, t)]
        return "+".join(on) if on else "none"

    @property
    def draws_anything(self) -> bool:
        return self.boxes or self.labels or self.info


def parse_overlay(text: str | None) -> OverlayOptions:
    """`None` -> everything; `none` -> the raw frame; else a comma list of known tokens."""
    if text is None or text.strip() == "":
        return OverlayOptions()
    cleaned = text.strip().lower()
    if cleaned == "none":
        return OverlayOptions(False, False, False)
    tokens = {t.strip() for t in cleaned.split(",") if t.strip()}
    unknown = sorted(tokens - set(OVERLAY_TOKENS))
    if unknown:
        raise ValueError(
            f"unknown overlay option(s) {', '.join(unknown)}; "
            f"valid: {', '.join(OVERLAY_TOKENS)} or 'none'"
        )
    return OverlayOptions(boxes="boxes" in tokens, labels="labels" in tokens, info="info" in tokens)


def overlay_from_key(key: str) -> OverlayOptions:
    return (
        OverlayOptions(False, False, False)
        if key == "none"
        else parse_overlay(key.replace("+", ","))
    )


def _draw_text(
    img: np.ndarray,
    text: str,
    origin: tuple[int, int],
    scale: float,
    thickness: int,
    fg: tuple[int, int, int],
    bg: tuple[int, int, int] | None,
) -> None:
    (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    x, y = origin
    if bg is not None:
        cv2.rectangle(img, (x, y - th - baseline), (x + tw, y + baseline), bg, cv2.FILLED)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, fg, thickness, cv2.LINE_AA)


def render_jpeg(
    analysed: AnalysedFrame,
    options: OverlayOptions,
    *,
    jpeg_quality: int,
    age_ms: float | None = None,
) -> bytes:
    """Annotate (on a copy) and JPEG-encode. `age_ms` is how old the analysis is; a stale
    frame is marked as such rather than shown as if it were live."""
    frame, result = analysed.frame, analysed.result
    meta = frame.meta
    pixels = np.frombuffer(frame.pixels.buffer(), dtype=np.uint8)
    try:
        img = pixels.reshape(meta.height_px, meta.width_px, frame.pixels.channels)
    except ValueError as exc:
        raise RenderError(f"cannot read frame pixels: {exc}") from exc
    stale_ms = age_ms if age_ms is not None and age_ms > STALE_AFTER_MS else None

    if options.draws_anything or stale_ms is not None:
        img = img.copy()  # frames are immutable: draw on a private copy
        short_side = min(meta.width_px, meta.height_px)
        thickness = max(1, round(short_side / 320))
        scale = max(0.4, short_side / 900)
        if options.boxes or options.labels:
            for d in result.detections:
                colour = CLASS_COLOURS.get(d.object_class, _DEFAULT_COLOUR)
                x1, y1, x2, y2 = (round(v) for v in (d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2))
                if options.boxes:
                    cv2.rectangle(img, (x1, y1), (x2, y2), colour, thickness)
                if options.labels:
                    label = f"{d.object_class.value} {d.confidence_ratio:.2f}"
                    _draw_text(
                        img,
                        label,
                        (x1, max(y1 - 4, round(14 * scale) + 4)),
                        scale * 0.7,
                        max(1, thickness - 1),
                        (0, 0, 0),
                        colour,
                    )
        if options.info:
            model = result.model
            line = (
                f"{meta.camera_id}  #{meta.frame_index}  {model.name}"
                f"{f' {model.input_size_px}px' if model.input_size_px else ''}  {model.device}  "
                f"{result.inference_latency_ms:.1f} ms  {len(result.detections)} det"
            )
            _draw_text(
                img,
                line,
                (8, round(22 * scale) + 4),
                scale * 0.7,
                max(1, thickness - 1),
                _HUD_COLOUR,
                (0, 0, 0),
            )
        if stale_ms is not None:
            _draw_text(
                img,
                f"STALE {stale_ms / 1000:.1f} s",
                (8, meta.height_px - 10),
                scale,
                thickness,
                (255, 255, 255),
                _STALE_COLOUR,
            )

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not ok:
        raise RenderError("JPEG encoding failed")
    return bytes(buf.tobytes())
