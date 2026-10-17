"""Building candidates and verification state for tests, without a pipeline."""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime

from vigil.config.schema.modules import TemporalConfig, VerificationConfig
from vigil.core.capabilities import Availability
from vigil.core.clock import ManualClock
from vigil.core.ids import UlidFactory
from vigil.core.units import ms_to_ns
from vigil.domain.camera import CameraHealth
from vigil.domain.enums import CameraState, EventType
from vigil.domain.event import AccumulatorSample, CandidateEvent, VerifiedEvent
from vigil.domain.observation import Observation, SubjectKey
from vigil.domain.scene import SceneState
from vigil.events.promotion import promote
from vigil.temporal.accumulator import Accumulator
from vigil.verification.ledger import SuppressionLedger
from vigil.verification.validators.context import Mute, VerificationState

CAMERA = "test-cam"
TEMPORAL = TemporalConfig()
DEFAULT_SUBJECT = SubjectKey("track", "1")


def ulids(seed: int = 7) -> UlidFactory:
    """A fresh factory. Two of these produce the *same* id from the same seed and clock, which
    is the point for a replay test and a trap everywhere else — see `SHARED_ULIDS`."""
    return UlidFactory(ManualClock(), random.Random(seed))


# One monotonic factory, as production has: ids from it sort in creation order and never repeat.
# `candidate()` uses this by default so distinct candidates get distinct ids.
SHARED_ULIDS = ulids(20261001)


def observation(
    *,
    subject: SubjectKey,
    t_ms: float,
    signal: float = 0.8,
    camera_id: str = CAMERA,
    module_id: str = "intrusion",
    zone_id: str | None = "yard",
) -> Observation:
    facts: dict[str, float | str | bool] = {}
    if zone_id is not None:
        facts["zone_id"] = zone_id
    return Observation(
        camera_id=camera_id,
        module_id=module_id,
        subject=subject,
        signal=signal,
        t_monotonic_ns=ms_to_ns(t_ms),
        facts=facts,
    )


def accumulate(
    *,
    subject: SubjectKey = DEFAULT_SUBJECT,
    signals: Sequence[float] = (0.8,) * 13,
    step_ms: float = 100.0,
    camera_id: str = CAMERA,
    module_id: str = "intrusion",
    config: TemporalConfig = TEMPORAL,
    zone_id: str | None = "yard",
) -> Accumulator:
    accumulator = Accumulator(
        camera_id=camera_id,
        module_id=module_id,
        subject=subject,
        config=config,
        support_slot_ms=step_ms,
    )
    for i, signal in enumerate(signals):
        accumulator.observe(
            observation(
                subject=subject,
                t_ms=i * step_ms,
                signal=signal,
                camera_id=camera_id,
                module_id=module_id,
                zone_id=zone_id,
            )
        )
    return accumulator


def candidate(
    *,
    subject: SubjectKey = DEFAULT_SUBJECT,
    event_type: EventType = EventType.INTRUSION,
    camera_id: str = CAMERA,
    module_id: str = "intrusion",
    signals: Sequence[float] = (0.8,) * 13,
    step_ms: float = 100.0,
    zone_id: str | None = "yard",
    factory: UlidFactory | None = None,
) -> CandidateEvent:
    accumulator = accumulate(
        subject=subject,
        signals=signals,
        step_ms=step_ms,
        camera_id=camera_id,
        module_id=module_id,
        zone_id=zone_id,
    )
    now_ns = ms_to_ns((len(signals) - 1) * step_ms)
    return promote(
        state=accumulator.state_at(now_ns),
        event_type=event_type,
        observations=accumulator.window(),
        promoted_monotonic_ns=now_ns,
        promoted_wall_utc=datetime(2026, 1, 1, 2, 41, tzinfo=UTC),
        ulids=factory or SHARED_ULIDS,
    )


def trace_of(accumulator: Accumulator) -> tuple[AccumulatorSample, ...]:
    return accumulator.trace()


def healthy(camera_id: str = CAMERA) -> CameraHealth:
    return CameraHealth(
        camera_id=camera_id,
        state=CameraState.ONLINE,
        since_wall_utc=datetime(2026, 1, 1, tzinfo=UTC),
        stream_epoch=1,
        reconnect_attempts=0,
        last_frame_wall_utc=datetime(2026, 1, 1, tzinfo=UTC),
        measured_fps=10.0,
        frames_received=100,
        frames_skipped=0,
        frames_dropped=0,
        decode_errors=0,
        detail=None,
    )


def unhealthy(state: CameraState, camera_id: str = CAMERA) -> CameraHealth:
    base = healthy(camera_id)
    return CameraHealth(
        camera_id=base.camera_id,
        state=state,
        since_wall_utc=base.since_wall_utc,
        stream_epoch=base.stream_epoch,
        reconnect_attempts=1,
        last_frame_wall_utc=base.last_frame_wall_utc,
        measured_fps=base.measured_fps,
        frames_received=base.frames_received,
        frames_skipped=base.frames_skipped,
        frames_dropped=base.frames_dropped,
        decode_errors=base.decode_errors,
        detail="injected for a test",
    )


def state(
    *,
    ledger: SuppressionLedger | None = None,
    config: VerificationConfig | None = None,
    now_ms: float = 1200.0,
    scene: SceneState | None = None,
    camera_health: CameraHealth | None = None,
    availability: Availability = Availability.ACTIVE,
    missing_optional: tuple[str, ...] = (),
    mutes: tuple[Mute, ...] = (),
    instability: dict[str, int] | None = None,
) -> VerificationState:
    resolved = config or VerificationConfig()
    return VerificationState(
        config=resolved,
        ledger=ledger if ledger is not None else SuppressionLedger(resolved),
        now_monotonic_ns=ms_to_ns(now_ms),
        camera_health=camera_health if camera_health is not None else healthy(),
        scene=scene,
        module_availability=availability,
        missing_optional=missing_optional,
        mutes=mutes,
        instability_since_monotonic_ns=instability or {},
    )


def verified(
    *,
    cand: CandidateEvent | None = None,
    confidence: float = 0.92,
    at_ms: float = 1200.0,
) -> VerifiedEvent:
    return VerifiedEvent(
        candidate=cand or candidate(),
        verdicts=(),
        confidence_ratio=confidence,
        verified_monotonic_ns=ms_to_ns(at_ms),
    )
