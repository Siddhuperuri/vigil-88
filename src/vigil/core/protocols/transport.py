"""Video transport interface (08 §4). Exists so MJPEG -> WebRTC is a swap, not a rewrite."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.domain.frame import FrameMeta


@dataclass(frozen=True, slots=True)
class StreamKey:
    """One encoded stream: a camera with one specific overlay set.

    Overlays are drawn server-side (the pixels and the detections are paired there, so the boxes
    cannot drift from the video). Each distinct overlay set is encoded once and fanned out to
    every viewer of it, so N browser tabs cost one encode, not N.
    """

    camera_id: str
    overlay: str  # canonical, e.g. "boxes+labels"; "none" is the raw frame


class VideoTransport(Protocol):
    def publish(self, key: StreamKey, jpeg: bytes, meta: FrameMeta) -> None: ...

    def viewer_count(self, key: StreamKey) -> int:
        """Encoders run only while someone is watching (08 §4)."""
        ...

    def active_keys(self) -> tuple[StreamKey, ...]:
        """Streams with at least one viewer: the only ones worth encoding."""
        ...

    def close(self) -> None: ...
