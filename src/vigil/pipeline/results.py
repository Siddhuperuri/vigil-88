"""Latest detection result per camera, published as immutable values.

The API/UI reads from here; it never touches the detector or any pipeline lock beyond this
one short critical section (01 §8 invariant 5).
"""

from __future__ import annotations

import threading

from vigil.domain.detection import DetectionSet


class LatestDetections:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_camera: dict[str, DetectionSet] = {}

    def update(self, result: DetectionSet) -> None:
        with self._lock:
            self._by_camera[result.frame_meta.camera_id] = result

    def get(self, camera_id: str) -> DetectionSet | None:
        with self._lock:
            return self._by_camera.get(camera_id)

    def snapshot(self) -> dict[str, DetectionSet]:
        with self._lock:
            return dict(self._by_camera)
