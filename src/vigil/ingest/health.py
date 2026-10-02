"""Camera health state machine (07 §5).

States and the transitions between them are an explicit table: an illegal transition is a
LifecycleError, not a silent overwrite. DEGRADED is entered by `evaluate()` (stall, low fps,
decode errors) and exits when the condition clears. ANALYSIS_SUSPENDED exists in the table
for the degradation ladder (P1+); nothing in P0 enters it.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from datetime import datetime

from vigil.config.schema.pipeline import HealthConfig, IngestConfig
from vigil.core.bus import EventBus
from vigil.core.clock import Clock
from vigil.core.errors import LifecycleError
from vigil.core.ids import validate_camera_id
from vigil.core.units import MS_PER_S, NS_PER_S, ms_to_ns, ns_to_ms
from vigil.domain.camera import CameraHealth, CameraStateChanged
from vigil.domain.enums import CameraState

S = CameraState
_ALL_STOPS = frozenset({S.RECONNECTING, S.OFFLINE, S.FAILED, S.DISABLED})

TRANSITIONS: Mapping[CameraState, frozenset[CameraState]] = {
    S.DISABLED: frozenset({S.INITIALIZING}),
    S.INITIALIZING: frozenset({S.ONLINE, S.RECONNECTING, S.FAILED, S.OFFLINE, S.DISABLED}),
    S.ONLINE: frozenset({S.DEGRADED, S.ANALYSIS_SUSPENDED}) | _ALL_STOPS,
    S.DEGRADED: frozenset({S.ONLINE, S.ANALYSIS_SUSPENDED}) | _ALL_STOPS,
    S.ANALYSIS_SUSPENDED: frozenset({S.ONLINE, S.DEGRADED}) | _ALL_STOPS,
    S.RECONNECTING: frozenset({S.ONLINE, S.FAILED, S.OFFLINE, S.DISABLED}),
    S.OFFLINE: frozenset({S.INITIALIZING, S.DISABLED}),
    S.FAILED: frozenset({S.INITIALIZING, S.DISABLED}),
}


class CameraHealthTracker:
    """Thread-safe. Mutated by the capture worker and the watchdog, read by anyone."""

    def __init__(
        self,
        camera_id: str,
        *,
        clock: Clock,
        ingest: IngestConfig,
        health: HealthConfig,
        bus: EventBus | None = None,
        watch_stall: bool = True,
        initial_state: CameraState = CameraState.INITIALIZING,
    ) -> None:
        self.camera_id = validate_camera_id(camera_id)
        self._clock = clock
        self._ingest = ingest
        self._cfg = health
        self._bus = bus
        self._watch_stall = watch_stall
        self._lock = threading.Lock()

        self._state = initial_state
        self._detail: str | None = None
        self._since_wall: datetime = clock.wall_utc()
        self._epoch = 0
        self._reconnect_attempts = 0
        self._received = 0
        self._skipped = 0
        self._dropped = 0
        self._decode_errors = 0
        self._last_frame_wall: datetime | None = None
        self._last_frame_mono: int | None = None
        self._online_mono: int | None = None
        self._expected_fps: float | None = None
        self._frame_times: deque[int] = deque(maxlen=health.fps_window_frames)
        self._error_times: deque[int] = deque()
        self._degraded_by_evaluate = False

    # ------------------------------------------------------------ transitions

    def _move(self, new: CameraState, detail: str | None) -> CameraStateChanged | None:
        """Caller holds the lock. Returns the event to publish after releasing it."""
        old = self._state
        if new == old:
            self._detail = detail if detail is not None else self._detail
            return None
        if new not in TRANSITIONS[old]:
            raise LifecycleError(
                f"illegal camera state transition {old.value} -> {new.value}",
                context={"camera_id": self.camera_id},
            )
        self._state = new
        self._detail = detail
        self._since_wall = self._clock.wall_utc()
        return CameraStateChanged(self.camera_id, old, new, detail, self._since_wall)

    def _publish(self, event: CameraStateChanged | None) -> None:
        if event is not None and self._bus is not None:
            self._bus.publish(event)

    def transition(self, new: CameraState, detail: str | None = None) -> None:
        with self._lock:
            event = self._move(new, detail)
        self._publish(event)

    def mark_initializing(self, detail: str | None = None) -> None:
        self.transition(S.INITIALIZING, detail)

    def mark_online(self, *, epoch: int, expected_fps: float | None) -> None:
        with self._lock:
            self._epoch = epoch
            self._reconnect_attempts = 0
            self._expected_fps = expected_fps if expected_fps and expected_fps > 0 else None
            self._frame_times.clear()
            self._error_times.clear()
            self._last_frame_mono = None
            self._online_mono = self._clock.monotonic_ns()
            self._degraded_by_evaluate = False
            event = self._move(S.ONLINE, None)
        self._publish(event)

    def mark_reconnecting(self, detail: str) -> None:
        with self._lock:
            self._reconnect_attempts += 1
            event = self._move(S.RECONNECTING, detail)
        self._publish(event)

    def mark_failed(self, detail: str) -> None:
        self.transition(S.FAILED, detail)

    def mark_offline(self, detail: str) -> None:
        self.transition(S.OFFLINE, detail)

    def mark_disabled(self, detail: str | None = None) -> None:
        self.transition(S.DISABLED, detail)

    # ------------------------------------------------------------ counters

    def record_frame(self, t_monotonic_ns: int, wall_utc: datetime) -> None:
        with self._lock:
            self._received += 1
            self._last_frame_mono = t_monotonic_ns
            self._last_frame_wall = wall_utc
            self._frame_times.append(t_monotonic_ns)

    def record_skipped(self) -> None:
        with self._lock:
            self._skipped += 1

    def record_dropped(self) -> None:
        with self._lock:
            self._dropped += 1

    def record_decode_error(self, t_monotonic_ns: int) -> None:
        with self._lock:
            self._decode_errors += 1
            self._error_times.append(t_monotonic_ns)

    # ------------------------------------------------------------ evaluation

    def _measured_fps(self) -> float | None:
        if len(self._frame_times) < self._cfg.min_frames_for_fps:
            return None
        span_ns = self._frame_times[-1] - self._frame_times[0]
        if span_ns <= 0:
            return None
        return (len(self._frame_times) - 1) / (span_ns / NS_PER_S)

    def evaluate(self, now_monotonic_ns: int | None = None) -> None:
        """Periodic check (watchdog). Enters/leaves DEGRADED for stall, low fps, decode errors.

        `now` defaults to this camera's own clock: a replay camera runs on a PTS-driven clock
        that is not the application clock.
        """
        now_ns = self._clock.monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
        with self._lock:
            if self._state not in (S.ONLINE, S.DEGRADED):
                return
            reasons = self._degraded_reasons(now_ns)
            event: CameraStateChanged | None = None
            if reasons and self._state is S.ONLINE:
                event = self._move(S.DEGRADED, "; ".join(reasons))
                self._degraded_by_evaluate = True
            elif not reasons and self._state is S.DEGRADED and self._degraded_by_evaluate:
                event = self._move(S.ONLINE, "recovered")
                self._degraded_by_evaluate = False
            elif reasons and self._state is S.DEGRADED:
                self._detail = "; ".join(reasons)
        self._publish(event)

    def _degraded_reasons(self, now_ns: int) -> list[str]:
        reasons: list[str] = []
        if self._watch_stall:
            baseline = self._last_frame_mono if self._last_frame_mono is not None else self._online_mono
            if baseline is not None:
                interval_ms = MS_PER_S / self._expected_fps if self._expected_fps else 0.0
                threshold_ms = max(
                    float(self._ingest.stall_timeout_ms),
                    self._ingest.stall_interval_multiplier * interval_ms,
                )
                silent_ms = ns_to_ms(now_ns - baseline)
                if silent_ms > threshold_ms:
                    reasons.append(f"stalled: no frame for {silent_ms:.0f} ms")
            fps = self._measured_fps()
            if (
                fps is not None
                and self._expected_fps is not None
                and fps < self._cfg.min_fps_ratio * self._expected_fps
            ):
                reasons.append(
                    f"low fps: {fps:.1f} measured vs {self._expected_fps:.1f} expected"
                )
        window_ns = ms_to_ns(self._cfg.decode_error_window_ms)
        while self._error_times and now_ns - self._error_times[0] > window_ns:
            self._error_times.popleft()
        if len(self._error_times) > self._cfg.max_decode_errors_per_window:
            reasons.append(f"{len(self._error_times)} decode errors in the last window")
        return reasons

    # ------------------------------------------------------------ read side

    @property
    def state(self) -> CameraState:
        with self._lock:
            return self._state

    @property
    def stream_epoch(self) -> int:
        with self._lock:
            return self._epoch

    def snapshot(self) -> CameraHealth:
        with self._lock:
            return CameraHealth(
                camera_id=self.camera_id,
                state=self._state,
                since_wall_utc=self._since_wall,
                stream_epoch=self._epoch,
                reconnect_attempts=self._reconnect_attempts,
                last_frame_wall_utc=self._last_frame_wall,
                measured_fps=self._measured_fps(),
                frames_received=self._received,
                frames_skipped=self._skipped,
                frames_dropped=self._dropped,
                decode_errors=self._decode_errors,
                detail=self._detail,
            )
