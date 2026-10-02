"""Latest analysed frame per camera, published as immutable values.

The stream and API read from here; they never touch the detector or any pipeline lock beyond
this one short critical section (01 §8 invariant 5). A Frame is immutable, so holding one
here costs a reference, not a copy.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from vigil.domain.detection import DetectionSet
from vigil.domain.frame import Frame


@dataclass(frozen=True, slots=True)
class AnalysedFrame:
    """A frame and the detections made on exactly that frame: the pair is what makes a
    server-side overlay correct by construction (08 §4)."""

    frame: Frame
    result: DetectionSet
    completed_monotonic_ns: int  # when inference finished, on the application clock


class LatestDetections:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, AnalysedFrame] = {}
        self._versions: dict[str, int] = {}

    def publish(self, frame: Frame, result: DetectionSet, completed_monotonic_ns: int) -> None:
        cam = result.frame_meta.camera_id
        with self._lock:
            self._latest[cam] = AnalysedFrame(frame, result, completed_monotonic_ns)
            self._versions[cam] = self._versions.get(cam, 0) + 1

    def latest(self, camera_id: str) -> AnalysedFrame | None:
        with self._lock:
            return self._latest.get(camera_id)

    def get(self, camera_id: str) -> DetectionSet | None:
        analysed = self.latest(camera_id)
        return analysed.result if analysed is not None else None

    def version(self, camera_id: str) -> int:
        """Increments on every publish; lets a consumer skip work when nothing is new."""
        with self._lock:
            return self._versions.get(camera_id, 0)

    def snapshot(self) -> dict[str, DetectionSet]:
        with self._lock:
            return {cam: a.result for cam, a in self._latest.items()}
