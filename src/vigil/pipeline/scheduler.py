"""Inference admission control (04 §5.1).

Each live camera has a token bucket refilled at its `target_inference_fps`; a global bucket
refilled at `pipeline.global_inference_fps` caps the total. Frames that arrive while a camera
has no token are simply left in its latest-frame slot and overwritten by the next one, which
is how they get counted as *skipped* (not inferred) rather than *dropped* (lost).

Eligible cameras are served by `(priority desc, time since last served desc)`. The second
term guarantees a low-priority camera is eventually served instead of starved. Lossless
channels (file replay) bypass admission entirely: a replay that skips frames is not
reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vigil.core.clock import Clock
from vigil.core.units import NS_PER_S

_FULL = 1.0
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class CameraPlan:
    camera_id: str
    rate_fps: float
    priority: int
    lossless: bool


class InferenceScheduler:
    def __init__(
        self,
        plans: Sequence[CameraPlan],
        *,
        global_fps: float,
        global_burst: int,
        clock: Clock,
    ) -> None:
        if global_fps <= 0 or global_burst < 1:
            raise ValueError("global_fps must be > 0 and global_burst >= 1")
        self._clock = clock
        self._plans = {p.camera_id: p for p in plans}
        now = clock.monotonic_ns()
        self._tokens = {p.camera_id: _FULL for p in plans}
        self._refilled = {p.camera_id: now for p in plans}
        # A sequence number, not a timestamp: two cameras served in the same instant would tie,
        # and ties would always resolve in insertion order, quietly favouring the first camera.
        self._last_served = {p.camera_id: -1 for p in plans}
        self._serve_seq = 0
        self._global_fps = global_fps
        self._global_cap = float(global_burst)
        self._global_tokens = self._global_cap
        self._global_refilled = now

    def _refill(self, now_ns: int) -> None:
        elapsed_g = max(0, now_ns - self._global_refilled) / NS_PER_S
        self._global_tokens = min(
            self._global_cap, self._global_tokens + elapsed_g * self._global_fps
        )
        self._global_refilled = now_ns
        for cam, plan in self._plans.items():
            if plan.lossless:
                continue
            elapsed = max(0, now_ns - self._refilled[cam]) / NS_PER_S
            self._tokens[cam] = min(_FULL, self._tokens[cam] + elapsed * plan.rate_fps)
            self._refilled[cam] = now_ns

    def order(self) -> list[str]:
        """Cameras that may be served right now, most deserving first."""
        now = self._clock.monotonic_ns()
        self._refill(now)
        eligible = [
            cam
            for cam, plan in self._plans.items()
            if plan.lossless or self._tokens[cam] >= _FULL - _EPSILON
        ]
        eligible.sort(key=lambda c: (-self._plans[c].priority, self._last_served[c]))
        return eligible

    def global_capacity(self) -> int:
        """How many frames the global ceiling allows right now."""
        self._refill(self._clock.monotonic_ns())
        return int(self._global_tokens + _EPSILON)

    def consume(self, camera_id: str) -> None:
        """Record that a frame from this camera was taken for inference."""
        plan = self._plans[camera_id]
        if not plan.lossless:
            self._tokens[camera_id] = max(0.0, self._tokens[camera_id] - _FULL)
            self._global_tokens = max(0.0, self._global_tokens - _FULL)
        self._last_served[camera_id] = self._serve_seq
        self._serve_seq += 1
