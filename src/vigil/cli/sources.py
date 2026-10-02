"""Parsing of ad-hoc `--source` arguments into camera config mappings.

webcam:<index>          a local webcam
synthetic[:<frames>]    the deterministic test pattern
rtsp://... | rtsps://...  accepted by the grammar; the application reports the camera
                        as FAILED because RTSP capture is not implemented yet
"""

from __future__ import annotations

import re

_WEBCAM_RE = re.compile(r"^webcam:(\d+)$")
_SYNTH_RE = re.compile(r"^synthetic(?::(\d+))?$")
_RTSP_PREFIXES = ("rtsp://", "rtsps://")


class SourceSpecError(ValueError):
    """The --source text is not in a recognised form."""


def parse_source(text: str, ordinal: int) -> dict[str, object]:
    if m := _WEBCAM_RE.match(text):
        index = int(m.group(1))
        return {
            "camera_id": f"cli-webcam-{index}",
            "name": f"Webcam {index} (from --source)",
            "source": {"kind": "webcam", "device_index": index},
        }
    if m := _SYNTH_RE.match(text):
        source: dict[str, object] = {"kind": "synthetic"}
        if m.group(1):
            source["frame_count"] = int(m.group(1))
        return {
            "camera_id": f"cli-synthetic-{ordinal}",
            "name": "Synthetic test pattern (from --source)",
            "source": source,
        }
    if text.startswith(_RTSP_PREFIXES):
        return {
            "camera_id": f"cli-rtsp-{ordinal}",
            "name": "RTSP (from --source)",
            "source": {"kind": "rtsp", "url": text},
        }
    raise SourceSpecError(
        f"unrecognised source {text!r}: use webcam:<index>, synthetic[:<frames>] or rtsp://..."
    )


def parse_sources(texts: list[str]) -> list[dict[str, object]]:
    return [parse_source(t, i) for i, t in enumerate(texts)]
