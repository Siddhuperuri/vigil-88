"""Video transport interface (08 §4). Exists so MJPEG -> WebRTC is a swap, not a rewrite."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.domain.frame import FrameMeta


class VideoTransport(Protocol):
    def publish(self, camera_id: str, jpeg: bytes, meta: FrameMeta) -> None: ...

    def viewer_count(self, camera_id: str) -> int:
        """Encoders run only while someone is watching (08 §4)."""
        ...

    def close(self) -> None: ...
