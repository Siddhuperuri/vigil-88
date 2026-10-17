"""The eleven validators (06 §6), each tested in isolation."""

from __future__ import annotations

import pytest

from tests.support.candidates import CAMERA, candidate, state, unhealthy
from tests.support.scenes import SceneStateBuilder
from vigil.config.schema.modules import VerificationConfig
from vigil.core.capabilities import Availability
from vigil.core.units import ms_to_ns
from vigil.domain.enums import (
    CameraState,
    EventType,
    SceneFact,
    TrackFlag,
    ValidatorOutcome,
    ZoneKind,
)
from vigil.domain.observation import SubjectKey
from vigil.verification.ledger import SuppressionLedger, dedup_key
from vigil.verification.validators.chain import (
    DEFAULT_CHAIN,
    validate_camera_health,
    validate_capability_caveat,
    validate_co_requirement,
    validate_cooldown,
    validate_duplicate,
    validate_motion_sanity,
    validate_operator_suppression,
    validate_rate_limit,
    validate_scene_stability,
    validate_track_quality,
    validate_zone_gate,
)
from vigil.verification.validators.context import Mute

TRACK_1 = SubjectKey("track", "1")


def scene_with_track(**track: object) -> SceneStateBuilder:
    builder = SceneStateBuilder(camera_id=CAMERA).with_zone("yard", ZoneKind.RESTRICTED)
    defaults: dict[str, object] = {
        "at": (0.7, 0.6),
        "hits": 10,
        "age_frames": 10,
        "in_zones": ["yard"],
        "first_seen_ms": 0.0,
    }
    return builder.with_track(1, **{**defaults, **track}).at_time(1200.0)  # type: ignore[arg-type]


# ------------------------------------------------------------------ chain shape


def test_the_chain_has_the_eleven_validators_in_the_documented_order() -> None:
    assert [v.__name__ for v in DEFAULT_CHAIN] == [
        "validate_camera_health",
        "validate_scene_stability",
        "validate_track_quality",
        "validate_motion_sanity",
        "validate_zone_gate",
        "validate_co_requirement",
        "validate_duplicate",
        "validate_cooldown",
        "validate_rate_limit",
        "validate_operator_suppression",
        "validate_capability_caveat",
    ]


def test_every_verdict_carries_a_reason_including_passes() -> None:
    """Without reasons on passes you cannot tell a validator that passed from one that never
    executed (06 §6)."""
    cand = candidate()
    verification_state = state(scene=scene_with_track().build())
    for validator in DEFAULT_CHAIN:
        verdict = validator(cand, verification_state)
        assert verdict.reason.strip(), validator.__name__
        assert verdict.validator_id


# ------------------------------------------------------------------ 1. camera health


def test_a_healthy_camera_passes() -> None:
    verdict = validate_camera_health(candidate(), state())
    assert verdict.outcome is ValidatorOutcome.PASS


@pytest.mark.parametrize(
    "camera_state",
    [
        CameraState.RECONNECTING,
        CameraState.DEGRADED,
        CameraState.OFFLINE,
        CameraState.FAILED,
        CameraState.ANALYSIS_SUSPENDED,
    ],
)
def test_an_unhealthy_camera_rejects(camera_state: CameraState) -> None:
    verdict = validate_camera_health(candidate(), state(camera_health=unhealthy(camera_state)))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert camera_state.value in verdict.reason


def test_a_stale_frame_rejects() -> None:
    """Corrupted temporal spacing makes any judgement built on it unsound."""
    verdict = validate_camera_health(
        candidate(), state(scene=scene_with_track().build(), now_ms=10_000.0)
    )
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "old" in verdict.reason


def test_a_fresh_frame_passes() -> None:
    verdict = validate_camera_health(
        candidate(), state(scene=scene_with_track().build(), now_ms=1300.0)
    )
    assert verdict.outcome is ValidatorOutcome.PASS


# ------------------------------------------------------------------ 2. scene stability


def test_a_stable_scene_passes() -> None:
    verdict = validate_scene_stability(candidate(), state(scene=scene_with_track().build()))
    assert verdict.outcome is ValidatorOutcome.PASS


def test_an_unstable_scene_rejects() -> None:
    """Lights switching on: the largest single source of correlated false positives."""
    scene = scene_with_track().with_stability(0.2).build()
    verdict = validate_scene_stability(candidate(), state(scene=scene))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "stability" in verdict.reason


def test_the_blackout_window_after_instability_rejects() -> None:
    scene = scene_with_track().build()
    verdict = validate_scene_stability(
        candidate(),
        state(scene=scene, now_ms=1200.0, instability={CAMERA: ms_to_ns(1000.0)}),
    )
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "blackout" in verdict.reason


def test_after_the_blackout_window_passes() -> None:
    scene = scene_with_track().at_time(6000.0).build()
    verdict = validate_scene_stability(
        candidate(),
        state(scene=scene, now_ms=6000.0, instability={CAMERA: ms_to_ns(1000.0)}),
    )
    assert verdict.outcome is ValidatorOutcome.PASS


def test_unknown_stability_is_not_treated_as_instability() -> None:
    """Fail closed means reject what we know is bad, not everything we cannot measure."""
    scene = scene_with_track().with_stability(0.0).with_degraded_fact(SceneFact.STABILITY).build()
    verdict = validate_scene_stability(candidate(), state(scene=scene))
    assert verdict.outcome is ValidatorOutcome.PASS


# ------------------------------------------------------------------ 3. track quality


def test_a_well_tracked_subject_passes() -> None:
    verdict = validate_track_quality(candidate(), state(scene=scene_with_track().build()))
    assert verdict.outcome is ValidatorOutcome.PASS


def test_a_mostly_predicted_track_rejects() -> None:
    scene = scene_with_track(hits=6, age_frames=60).build()
    verdict = validate_track_quality(candidate(), state(scene=scene))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "mostly prediction" in verdict.reason


def test_a_young_track_defers_rather_than_rejecting() -> None:
    """DEFER means "not yet decidable": the answer may simply not be knowable yet (04 §3.6)."""
    scene = scene_with_track(first_seen_ms=1100.0).build()
    verdict = validate_track_quality(candidate(), state(scene=scene))
    assert verdict.outcome is ValidatorOutcome.DEFER
    assert "more history" in verdict.reason


def test_a_subject_absent_from_the_scene_rejects() -> None:
    scene = SceneStateBuilder(camera_id=CAMERA).with_zone("yard").at_time(1200.0).build()
    verdict = validate_track_quality(candidate(), state(scene=scene))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "not present" in verdict.reason


def test_a_non_track_subject_is_not_this_validator_s_business() -> None:
    cand = candidate(subject=SubjectKey("zone", "dock"))
    verdict = validate_track_quality(cand, state(scene=scene_with_track().build()))
    assert verdict.outcome is ValidatorOutcome.PASS
    assert "not a track" in verdict.reason


# ------------------------------------------------------------------ 4. motion sanity


def test_plausible_motion_passes() -> None:
    scene = scene_with_track(velocity_px_s=(46.0, 0.0)).build()
    verdict = validate_motion_sanity(candidate(), state(scene=scene))
    assert verdict.outcome is ValidatorOutcome.PASS


def test_an_implied_teleport_rejects() -> None:
    """A person at 200 px/s across a 640 px frame is an ID switch, not a sprinter."""
    scene = scene_with_track(velocity_px_s=(4000.0, 0.0)).build()
    verdict = validate_motion_sanity(candidate(), state(scene=scene))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "identity switch" in verdict.reason


def test_an_already_flagged_track_rejects() -> None:
    scene = scene_with_track(flags=[TrackFlag.SUSPECT_MOTION]).build()
    verdict = validate_motion_sanity(candidate(), state(scene=scene))
    assert verdict.outcome is ValidatorOutcome.REJECT


def test_unknown_velocity_is_not_an_objection() -> None:
    verdict = validate_motion_sanity(candidate(), state(scene=scene_with_track().build()))
    assert verdict.outcome is ValidatorOutcome.PASS


def test_the_speed_bound_is_relative_to_the_box() -> None:
    """A pixel bound would mean different things for a near subject and a far one."""
    near = scene_with_track(size=(60.0, 400.0), velocity_px_s=(1000.0, 0.0)).build()
    far = scene_with_track(size=(15.0, 40.0), velocity_px_s=(1000.0, 0.0)).build()
    assert validate_motion_sanity(candidate(), state(scene=near)).outcome is ValidatorOutcome.PASS
    assert validate_motion_sanity(candidate(), state(scene=far)).outcome is ValidatorOutcome.REJECT


# ------------------------------------------------------------------ 5. zone gate


def test_a_subject_in_an_exclusion_zone_rejects() -> None:
    """The most effective false-positive suppression in practice is spatial."""
    builder = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard", ZoneKind.RESTRICTED)
        .with_zone(
            "road", ZoneKind.EXCLUSION, polygon=((0.4, 0.3), (0.99, 0.3), (0.99, 0.99), (0.4, 0.99))
        )
        .with_track(
            1, at=(0.7, 0.6), hits=10, age_frames=10, in_zones=["yard", "road"], first_seen_ms=0.0
        )
        .at_time(1200.0)
    )
    verdict = validate_zone_gate(candidate(), state(scene=builder.build()))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "exclusion zone" in verdict.reason


def test_a_subject_outside_every_exclusion_zone_passes() -> None:
    verdict = validate_zone_gate(candidate(), state(scene=scene_with_track().build()))
    assert verdict.outcome is ValidatorOutcome.PASS


def test_a_governing_zone_outside_its_schedule_rejects() -> None:
    from vigil.domain.scene import Schedule

    builder = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone(
            "yard", ZoneKind.RESTRICTED, schedule=Schedule(start_minute=18 * 60, end_minute=6 * 60)
        )
        .with_track(1, at=(0.7, 0.6), hits=10, age_frames=10, in_zones=["yard"], first_seen_ms=0.0)
        .at_time(1200.0)
    )
    builder.wall_epoch = builder.wall_epoch.replace(hour=14)
    verdict = validate_zone_gate(candidate(), state(scene=builder.build()))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "schedule" in verdict.reason


# ------------------------------------------------------------------ 6. co-requirement


def test_an_event_type_without_structural_requirements_passes() -> None:
    verdict = validate_co_requirement(candidate(), state())
    assert verdict.outcome is ValidatorOutcome.PASS


def test_a_structurally_impossible_candidate_rejects() -> None:
    """Catches a module that fired on a configuration that cannot exist."""
    cand = candidate(event_type=EventType.VEHICLE_COLLISION)
    verdict = validate_co_requirement(cand, state())
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "at least 2" in verdict.reason


# ------------------------------------------------------------------ 7. duplicate


def test_no_open_event_passes() -> None:
    verdict = validate_duplicate(candidate(), state())
    assert verdict.outcome is ValidatorOutcome.PASS


def test_an_open_event_for_the_same_situation_rejects() -> None:
    """A second event for one ongoing situation is a reporting failure; it extends instead."""
    config = VerificationConfig()
    ledger = SuppressionLedger(config)
    cand = candidate()
    ledger.record_open(
        dedup_key(cand.camera_id, cand.event_type, cand.subject),
        candidate_id=cand.candidate_id,
        t_monotonic_ns=0,
    )
    verdict = validate_duplicate(cand, state(ledger=ledger, config=config))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "already open" in verdict.reason


def test_an_open_event_for_a_different_subject_does_not_block() -> None:
    config = VerificationConfig()
    ledger = SuppressionLedger(config)
    other = candidate(subject=SubjectKey("track", "99"))
    ledger.record_open(
        dedup_key(other.camera_id, other.event_type, other.subject),
        candidate_id=other.candidate_id,
        t_monotonic_ns=0,
    )
    verdict = validate_duplicate(candidate(), state(ledger=ledger, config=config))
    assert verdict.outcome is ValidatorOutcome.PASS


# ------------------------------------------------------------------ 8. cooldown


def test_a_recent_resolution_rejects() -> None:
    config = VerificationConfig(cooldown_ms=60_000)
    ledger = SuppressionLedger(config)
    cand = candidate()
    key = dedup_key(cand.camera_id, cand.event_type, cand.subject)
    ledger.record_open(key, candidate_id=cand.candidate_id, t_monotonic_ns=0)
    ledger.close(key, ms_to_ns(1000.0))
    verdict = validate_cooldown(cand, state(ledger=ledger, config=config, now_ms=2000.0))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "cooldown" in verdict.reason


def test_after_the_cooldown_passes() -> None:
    config = VerificationConfig(cooldown_ms=60_000)
    ledger = SuppressionLedger(config)
    cand = candidate()
    key = dedup_key(cand.camera_id, cand.event_type, cand.subject)
    ledger.close(key, ms_to_ns(1000.0))
    verdict = validate_cooldown(cand, state(ledger=ledger, config=config, now_ms=70_000.0))
    assert verdict.outcome is ValidatorOutcome.PASS


# ------------------------------------------------------------------ 9. rate limit


def test_a_camera_under_its_limit_passes() -> None:
    verdict = validate_rate_limit(candidate(), state())
    assert verdict.outcome is ValidatorOutcome.PASS


def test_a_camera_at_its_limit_rejects() -> None:
    """An alert storm destroys the operator's ability to see anything."""
    config = VerificationConfig(max_events_per_window=3)
    ledger = SuppressionLedger(config)
    for i in range(3):
        ledger.record_open(
            dedup_key(CAMERA, EventType.INTRUSION, SubjectKey("track", str(i))),
            candidate_id=f"c{i}",
            t_monotonic_ns=ms_to_ns(float(i)),
        )
    verdict = validate_rate_limit(candidate(), state(ledger=ledger, config=config))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "limit" in verdict.reason


def test_the_rate_limit_is_per_camera() -> None:
    config = VerificationConfig(max_events_per_window=1)
    ledger = SuppressionLedger(config)
    ledger.record_open(
        dedup_key("other-cam", EventType.INTRUSION, TRACK_1),
        candidate_id="c0",
        t_monotonic_ns=0,
    )
    verdict = validate_rate_limit(candidate(), state(ledger=ledger, config=config))
    assert verdict.outcome is ValidatorOutcome.PASS


def test_events_outside_the_window_do_not_count() -> None:
    config = VerificationConfig(max_events_per_window=1, rate_limit_window_ms=1000.0)
    ledger = SuppressionLedger(config)
    ledger.record_open(
        dedup_key(CAMERA, EventType.INTRUSION, SubjectKey("track", "9")),
        candidate_id="c0",
        t_monotonic_ns=0,
    )
    verdict = validate_rate_limit(candidate(), state(ledger=ledger, config=config, now_ms=5000.0))
    assert verdict.outcome is ValidatorOutcome.PASS


# ------------------------------------------------------------------ 10. operator suppression


def test_no_mute_passes() -> None:
    assert validate_operator_suppression(candidate(), state()).outcome is ValidatorOutcome.PASS


def test_a_camera_mute_rejects() -> None:
    mute = Mute(camera_id=CAMERA, reason="contractors until 17:00")
    verdict = validate_operator_suppression(candidate(), state(mutes=(mute,)))
    assert verdict.outcome is ValidatorOutcome.REJECT
    assert "contractors" in verdict.reason


def test_a_mute_for_another_camera_does_not_apply() -> None:
    mute = Mute(camera_id="other-cam")
    assert (
        validate_operator_suppression(candidate(), state(mutes=(mute,))).outcome
        is ValidatorOutcome.PASS
    )


def test_a_mute_scoped_to_a_zone_applies_only_to_that_zone() -> None:
    covering = Mute(camera_id=CAMERA, zone_id="yard")
    other = Mute(camera_id=CAMERA, zone_id="dock")
    assert (
        validate_operator_suppression(candidate(), state(mutes=(covering,))).outcome
        is ValidatorOutcome.REJECT
    )
    assert (
        validate_operator_suppression(candidate(), state(mutes=(other,))).outcome
        is ValidatorOutcome.PASS
    )


def test_a_mute_scoped_to_another_event_type_does_not_apply() -> None:
    mute = Mute(camera_id=CAMERA, event_type=EventType.FIRE)
    assert (
        validate_operator_suppression(candidate(), state(mutes=(mute,))).outcome
        is ValidatorOutcome.PASS
    )


def test_an_expired_mute_does_not_apply() -> None:
    mute = Mute(camera_id=CAMERA, expires_monotonic_ns=ms_to_ns(1000.0))
    verdict = validate_operator_suppression(candidate(), state(mutes=(mute,), now_ms=2000.0))
    assert verdict.outcome is ValidatorOutcome.PASS


# ------------------------------------------------------------------ 11. capability caveat


def test_the_caveat_validator_never_rejects() -> None:
    for availability in (Availability.ACTIVE, Availability.DEGRADED):
        verdict = validate_capability_caveat(candidate(), state(availability=availability))
        assert verdict.outcome is ValidatorOutcome.PASS


def test_a_degraded_module_s_limitation_is_annotated() -> None:
    """The caveat must travel with the data (04 §4)."""
    verdict = validate_capability_caveat(
        candidate(),
        state(availability=Availability.DEGRADED, missing_optional=("velocity",)),
    )
    assert "DEGRADED" in verdict.reason
    assert "velocity" in verdict.reason
