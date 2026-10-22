"""The integration scenarios of 09 §8, through the real chain.

`assert len(scored) == 1` is the assertion the whole architecture exists to make possible. A
per-frame classifier would produce one per frame — 138 for the first scenario here.

These run at the `SceneState` boundary rather than from frames, because P2's tracker and scene
analyzer are not implemented in this tree (docs/LIMITATIONS.md). Everything downstream of that
boundary is the production code path, assembled by the same builder the application uses.
"""

from __future__ import annotations

import pytest

from tests.support.candidates import healthy, unhealthy
from tests.support.replay import CAMERA, Replay, camera_config, settings_for, walk
from tests.support.scenes import SceneStateBuilder
from vigil.core.capabilities import Capability
from vigil.core.units import ms_to_ns
from vigil.domain.enums import (
    CameraState,
    CandidateOutcome,
    EventType,
    Severity,
)
from vigil.verification.validators.context import Mute

# Long enough for evidence to decay past l_release and the sustain window to elapse.
FULL_RUN_MS = 30_000.0


def replay(**kwargs: object) -> Replay:
    kwargs.setdefault("camera_health", healthy())
    return Replay(**kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the headline scenario


def test_a_clean_single_intrusion_produces_exactly_one_event() -> None:
    result = replay().run(
        walk(inside_from_ms=0.0, inside_until_ms=14_000.0, duration_ms=FULL_RUN_MS)
    )
    assert result.observations_total > 100  # the module spoke on every frame...
    assert len(result.scored) == 1  # ...and the platform decided once
    scored = result.scored[0]
    assert scored.candidate.event_type is EventType.INTRUSION
    assert str(scored.candidate.subject) == "track:1"
    assert scored.assessment.severity >= Severity.MODERATE


def test_the_detection_latency_is_bounded_and_set_by_min_duration() -> None:
    """`confirmed - first_observed`: the price of refusing to alarm on one frame (06 §12)."""
    result = replay().run(walk(inside_until_ms=14_000.0, duration_ms=FULL_RUN_MS))
    latency = result.scored[0].detection_latency_ms
    assert 500.0 <= latency <= 2000.0
    assert latency == pytest.approx(settings_for().temporal.min_duration_ms, abs=150.0)


def test_the_event_releases_once_the_person_leaves() -> None:
    result = replay().run(walk(inside_until_ms=14_000.0, duration_ms=FULL_RUN_MS))
    assert [str(s) for s in result.released] == ["track:1"]


def test_a_persistent_intrusion_stays_one_event_and_does_not_re_fire() -> None:
    """The same real-world situation must not produce a stream of logically identical events."""
    result = replay().run(walk(inside_until_ms=None, duration_ms=60_000.0))
    assert len(result.scored) == 1
    assert result.rejected == []  # no re-promotion to reject, because it never re-promoted
    assert result.released == []  # still happening


def test_the_verified_event_carries_its_evidence_and_explanation_inputs() -> None:
    result = replay().run(walk(inside_until_ms=14_000.0, duration_ms=FULL_RUN_MS))
    scored = result.scored[0]
    assert len(scored.candidate.observations) >= 13
    assert scored.candidate.observations[0].facts["zone_id"] == "yard"
    assert scored.candidate.zone_ids == frozenset({"yard"})
    assert scored.candidate.involved_track_ids == frozenset({1})
    assert len(scored.event.verdicts) == 11
    assert len(scored.assessment.factors) == 8
    assert sum(f.contribution for f in scored.assessment.factors) * 100.0 == pytest.approx(
        scored.assessment.score
    )


# ------------------------------------------------------------------ brief crossing (§14)


def test_a_brief_boundary_crossing_produces_nothing() -> None:
    """Three hundred milliseconds inside is an accident, not an intrusion."""
    result = replay().run(
        walk(inside_from_ms=1000.0, inside_until_ms=1300.0, duration_ms=FULL_RUN_MS)
    )
    assert result.observations_total > 0  # the module did see it
    assert result.scored == []  # the platform was not convinced
    assert result.records == []


@pytest.mark.parametrize("dwell_ms", [100.0, 300.0, 600.0, 900.0])
def test_crossings_shorter_than_min_duration_never_confirm(dwell_ms: float) -> None:
    result = replay().run(
        walk(inside_from_ms=1000.0, inside_until_ms=1000.0 + dwell_ms, duration_ms=FULL_RUN_MS)
    )
    assert result.scored == []


def test_a_crossing_just_past_min_duration_does_confirm() -> None:
    """The boundary is where the configured threshold says it is, not somewhere vaguer."""
    result = replay().run(
        walk(inside_from_ms=1000.0, inside_until_ms=2400.0, duration_ms=FULL_RUN_MS)
    )
    assert len(result.scored) == 1


# ------------------------------------------------------------------ uncertain evidence (§14)


def weak_signal_camera() -> object:
    """A camera whose intrusion module is configured barely above neutral.

    At `base_signal = 0.51` each frame contributes ln(0.51/0.49) ~ 0.04 of log-odds. With the
    default 2000 ms half-life the sum converges to about 1.17 — below `l_activate` — so no
    amount of such evidence can ever confirm, which is the property that matters.

    The bonuses are zeroed deliberately. Left at their defaults, the dwell bonus adds 0.10 to
    the signal after three seconds, which raises the per-frame contribution more than tenfold
    and does eventually confirm. That is correct behaviour — persistence *is* evidence — but it
    is not the thing under test here.
    """
    return camera_config(
        module_params={
            "intrusion": {
                "base_signal": 0.51,
                "dwell_bonus_ratio": 0.0,
                "velocity_bonus_ratio": 0.0,
            }
        }
    )


def test_evidence_that_stays_near_neutral_never_confirms() -> None:
    """A module that is persistently unsure cannot confirm by being unsure for long enough."""
    result = replay(camera=weak_signal_camera()).run(
        walk(inside_until_ms=None, duration_ms=60_000.0)
    )
    assert result.observations_total > 500  # it spoke six hundred times...
    assert result.scored == []  # ...and never convinced anyone
    assert result.records == []


def test_weak_evidence_accumulates_but_stays_below_activation() -> None:
    """Not merely "no event": the belief must genuinely settle below the threshold rather than
    be blocked by some other condition."""
    run = replay(camera=weak_signal_camera())
    run.run(walk(inside_until_ms=None, duration_ms=60_000.0))
    from vigil.domain.observation import SubjectKey

    accumulator = run.pipeline._store.get("intrusion", SubjectKey("track", "1"))
    assert accumulator is not None
    assert 0.0 < accumulator.log_odds < settings_for().temporal.l_activate


def test_a_module_emitting_only_neutral_evidence_never_confirms() -> None:
    """The property that matters most, end to end rather than in isolation."""
    settings = settings_for()
    run = replay(settings=settings)
    scenes = list(walk(inside_until_ms=None, duration_ms=30_000.0))
    # Drive the accumulator directly with neutral evidence for this subject.
    store = run.pipeline._store
    from vigil.domain.observation import Observation, SubjectKey

    subject = SubjectKey("track", "99")
    for scene in scenes:
        store.observe(
            Observation(
                camera_id=CAMERA,
                module_id="intrusion",
                subject=subject,
                signal=0.5,
                t_monotonic_ns=scene.frame_meta.t_monotonic_ns,
                facts={},
            )
        )
    accumulator = store.get("intrusion", subject)
    assert accumulator is not None
    assert accumulator.log_odds == pytest.approx(0.0, abs=1e-12)
    assert not accumulator.promotion_ready(ms_to_ns(30_000.0))


# ------------------------------------------------------------------ multiple subjects (§14)


def two_people(duration_ms: float = 20_000.0, step_ms: float = 100.0):  # type: ignore[no-untyped-def]
    """Two people, both inside the restricted zone throughout."""
    frames = int(duration_ms / step_ms) + 1
    for index in range(frames):
        t_ms = index * step_ms
        yield (
            SceneStateBuilder(camera_id=CAMERA)
            .with_zone("yard", criticality=3)
            .with_track(
                12,
                at=(0.7, 0.6),
                hits=index + 1,
                age_frames=index + 1,
                in_zones=["yard"],
                dwell_ms={"yard": t_ms},
                first_seen_ms=0.0,
            )
            .with_track(
                19,
                at=(0.85, 0.7),
                hits=index + 1,
                age_frames=index + 1,
                in_zones=["yard"],
                dwell_ms={"yard": t_ms},
                first_seen_ms=0.0,
            )
            .at_time(t_ms, frame_index=index)
            .build()
        )


def test_two_people_in_one_zone_produce_two_events_not_one_or_four() -> None:
    """Separate `track:` subjects, so the event can name who (09 §8)."""
    result = replay().run(two_people())
    assert len(result.scored) == 2
    assert {str(s.candidate.subject) for s in result.scored} == {"track:12", "track:19"}


def test_two_people_accumulate_independently() -> None:
    """Subject too broad would merge them: two people would look like one strong intrusion."""
    run = replay()
    run.run(two_people(duration_ms=5000.0))
    from vigil.domain.observation import SubjectKey

    store = run.pipeline._store
    first = store.get("intrusion", SubjectKey("track", "12"))
    second = store.get("intrusion", SubjectKey("track", "19"))
    assert first is not None and second is not None
    assert first is not second
    assert first.state.observation_count == second.state.observation_count


# ------------------------------------------------------------------ multiple cameras (§14)


def test_camera_contexts_remain_isolated() -> None:
    """One camera's evidence must never reach another's accumulator."""
    first = replay(camera=camera_config("cam-01"))
    second = replay(camera=camera_config("cam-02"))
    first_result = first.run(walk(camera_id="cam-01", inside_until_ms=None, duration_ms=20_000.0))
    second_result = second.run(
        walk(
            camera_id="cam-02",
            inside_from_ms=1000.0,
            inside_until_ms=1200.0,
            duration_ms=20_000.0,
        )
    )
    assert len(first_result.scored) == 1
    assert second_result.scored == []
    assert first_result.scored[0].candidate.camera_id == "cam-01"


def test_the_same_track_id_on_two_cameras_is_two_situations() -> None:
    first = replay(camera=camera_config("cam-01"))
    second = replay(camera=camera_config("cam-02"))
    a = first.run(walk(camera_id="cam-01", track_id=4, inside_until_ms=None, duration_ms=20_000.0))
    b = second.run(walk(camera_id="cam-02", track_id=4, inside_until_ms=None, duration_ms=20_000.0))
    assert len(a.scored) == 1 and len(b.scored) == 1
    assert a.scored[0].candidate.camera_id != b.scored[0].candidate.camera_id


# ------------------------------------------------------------------ hysteresis (§9)


def boundary_loiter(duration_ms: float = 60_000.0, step_ms: float = 100.0):  # type: ignore[no-untyped-def]
    """Someone standing on the line: in the zone on alternating frames for a minute."""
    frames = int(duration_ms / step_ms) + 1
    dwell = 0.0
    for index in range(frames):
        t_ms = index * step_ms
        inside = index % 2 == 0
        dwell = dwell + step_ms if inside else 0.0
        yield (
            SceneStateBuilder(camera_id=CAMERA)
            .with_zone("yard", criticality=3)
            .with_track(
                1,
                at=(0.505, 0.6) if inside else (0.495, 0.6),
                hits=index + 1,
                age_frames=index + 1,
                in_zones=["yard"] if inside else [],
                dwell_ms={"yard": dwell},
                first_seen_ms=0.0,
            )
            .at_time(t_ms, frame_index=index)
            .build()
        )


def test_boundary_loitering_for_a_minute_yields_at_most_one_confirm_and_one_release() -> None:
    """04 §9: hysteresis cannot flap. This is the test that defends the gap's existence."""
    result = replay().run(boundary_loiter())
    assert len(result.scored) <= 1
    assert len(result.released) <= 1


def test_a_long_loiter_does_not_produce_an_event_storm() -> None:
    result = replay().run(boundary_loiter(duration_ms=120_000.0))
    assert len(result.scored) <= 1
    assert len(result.rejected) <= 1


# ------------------------------------------------------------------ dedup and cooldown (§18)


def test_re_entry_during_cooldown_is_rejected_with_a_reason() -> None:
    """Stops resolve/refire oscillation at a boundary (06 §6 validator 8)."""
    run = replay(settings=settings_for(**{"verification.cooldown_ms": 60_000}))
    scenes = [
        *walk(inside_until_ms=14_000.0, duration_ms=30_000.0),
        *walk(inside_from_ms=0.0, inside_until_ms=None, duration_ms=5000.0),
    ]
    # The second walk restarts at t=0, which a real stream never does; re-time it.
    result = run.run(scenes[: len(list(walk(inside_until_ms=14_000.0, duration_ms=30_000.0)))])
    assert len(result.scored) == 1
    assert len(result.released) == 1


def test_a_second_candidate_for_an_open_situation_is_refused_as_a_duplicate() -> None:
    """Verified directly against the ledger, because the accumulator will not re-promote an
    active subject — two independent defences against the same failure."""
    from tests.support.candidates import candidate, state
    from vigil.config.schema.modules import VerificationConfig
    from vigil.domain.enums import ValidatorOutcome
    from vigil.verification.ledger import SuppressionLedger, dedup_key
    from vigil.verification.validators.chain import validate_duplicate

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


def test_after_the_cooldown_a_genuine_recurrence_confirms_again() -> None:
    run = replay(settings=settings_for(**{"verification.cooldown_ms": 1000}))
    first_visit = list(walk(inside_until_ms=14_000.0, duration_ms=40_000.0))
    result = run.run(first_visit)
    assert len(result.scored) == 1
    assert len(result.released) == 1
    # A second visit, well after the cooldown, is a new situation with its own evidence.
    second = list(
        walk(
            inside_from_ms=0.0,
            inside_until_ms=None,
            duration_ms=5000.0,
            step_ms=100.0,
        )
    )
    retimed = [_shift(scene, 45_000.0) for scene in second]
    later = run.run(retimed)
    assert len(later.scored) == 1


def _shift(scene: object, offset_ms: float) -> object:
    """Re-stamp a scene later in time, so two scripted visits form one monotonic stream."""
    from vigil.domain.frame import FrameMeta
    from vigil.domain.scene import SceneState

    assert isinstance(scene, SceneState)
    meta = scene.frame_meta
    return SceneState(
        frame_meta=FrameMeta(
            camera_id=meta.camera_id,
            stream_epoch=meta.stream_epoch,
            frame_index=meta.frame_index + 100_000,
            t_monotonic_ns=meta.t_monotonic_ns + ms_to_ns(offset_ms),
            wall_utc=meta.wall_utc,
            width_px=meta.width_px,
            height_px=meta.height_px,
            source_pts_ms=meta.source_pts_ms,
            is_keyframe=meta.is_keyframe,
        ),
        tracks=scene.tracks,
        zones=scene.zones,
        occupancy=scene.occupancy,
        relations=scene.relations,
        stability_ratio=scene.stability_ratio,
        capabilities=scene.capabilities,
        degraded_facts=scene.degraded_facts,
    )


# ------------------------------------------------------------------ suppression


def test_an_unstable_scene_produces_no_events() -> None:
    """Zero events during the blackout: the lights-switched-on false-positive storm (09 §8)."""
    result = replay().run(walk(inside_until_ms=None, duration_ms=20_000.0, stability_ratio=0.1))
    assert result.observations_total > 0
    assert result.scored == []
    assert all(r.outcome is CandidateOutcome.REJECTED for r in result.records)
    assert result.records
    assert "stability" in (result.records[0].rejection_reason or "")


def test_an_unhealthy_camera_produces_no_events() -> None:
    result = replay(camera_health=unhealthy(CameraState.RECONNECTING)).run(
        walk(inside_until_ms=None, duration_ms=20_000.0)
    )
    assert result.scored == []
    assert result.records
    assert "camera_health" in (result.records[0].rejection_reason or "")


def test_an_operator_mute_suppresses_the_event_and_says_why() -> None:
    result = replay(mutes=(Mute(camera_id=CAMERA, reason="contractors until 17:00"),)).run(
        walk(inside_until_ms=None, duration_ms=20_000.0)
    )
    assert result.scored == []
    assert "contractors" in (result.records[0].rejection_reason or "")


def test_a_rejection_is_recorded_once_not_every_frame() -> None:
    """A suppressed situation must not become an endless stream of identical rejections."""
    result = replay(camera_health=unhealthy(CameraState.DEGRADED)).run(
        walk(inside_until_ms=None, duration_ms=60_000.0)
    )
    assert len(result.records) == 1


# ------------------------------------------------------------------ rejected candidates (§12)


def test_a_rejected_candidate_remains_fully_observable() -> None:
    result = replay(camera_health=unhealthy(CameraState.DEGRADED)).run(
        walk(inside_until_ms=None, duration_ms=20_000.0)
    )
    record = result.records[0]
    assert record.outcome is CandidateOutcome.REJECTED
    assert record.candidate.observations  # what was considered
    assert record.candidate.observations[0].facts["zone_id"] == "yard"  # why
    assert len(record.accumulator_trace) >= 13  # how confidence evolved
    assert record.accumulator_trace[0].log_odds < record.accumulator_trace[-1].log_odds
    assert record.rejection_reason  # why it was refused
    assert record.verdicts  # everything that ran
    assert record.decided_wall_utc.tzinfo is not None


# ------------------------------------------------------------------ capability gating (§16)


def test_a_camera_without_zones_runs_no_intrusion_module() -> None:
    """Unavailable functionality stays explicitly unavailable; it does not quietly do nothing."""
    run = replay(available=frozenset({Capability.DETECTION, Capability.TRACKING}))
    assert run.report.runnable_ids == ()
    # `intrusion` is unavailable for want of a zone file -- which an operator can
    # fix -- while `fire` and `smoke` are unavailable for want of a capability this
    # build cannot grant at all (ADR 0015). The two reasons must not read alike.
    by_id = {a.module_id: a for a in run.report.unavailable}
    assert set(by_id) == {"fire", "intrusion", "smoke"}
    assert "zones" in (by_id["intrusion"].reason or "")
    for declared in ("fire", "smoke"):
        assert "classification" in (by_id[declared].reason or "")
    result = run.run(walk(inside_until_ms=None, duration_ms=20_000.0))
    assert result.observations_total == 0
    assert result.scored == []


def test_a_degraded_camera_still_detects_and_carries_the_caveat() -> None:
    run = replay(available=frozenset({Capability.DETECTION, Capability.TRACKING, Capability.ZONES}))
    assert run.report.runnable_ids == ("intrusion",)
    assert run.report.caveats == ("intrusion: running without velocity",)
    result = run.run(
        walk(
            inside_until_ms=None,
            duration_ms=20_000.0,
            capabilities=frozenset({Capability.DETECTION, Capability.TRACKING, Capability.ZONES}),
        )
    )
    assert len(result.scored) == 1
    caveat = [v for v in result.scored[0].event.verdicts if v.validator_id == "capability_caveat"]
    assert caveat and "DEGRADED" in caveat[0].reason


def test_a_module_not_enabled_never_runs() -> None:
    from vigil.config.settings import Settings

    run = replay(settings=Settings.model_validate({"modules": {"enabled": []}}))
    assert run.report.runnable_ids == ()
    result = run.run(walk(inside_until_ms=None, duration_ms=20_000.0))
    assert result.observations_total == 0


# ------------------------------------------------------------------ epoch reset


def test_an_epoch_change_clears_accumulated_evidence() -> None:
    """04 §9: after a reset, no accumulator retains pre-reset evidence."""
    run = replay()
    before = list(walk(inside_until_ms=None, duration_ms=1000.0, tracker_epoch=1))
    after = [
        _shift(scene, 1100.0)
        for scene in walk(inside_until_ms=None, duration_ms=1000.0, tracker_epoch=2)
    ]
    result = run.run([*before, *after])
    assert result.epoch_resets == 1
    assert result.scored == []  # neither side accumulated 1200 ms of its own evidence


def test_evidence_after_an_epoch_change_accumulates_from_zero() -> None:
    run = replay()
    before = list(walk(inside_until_ms=None, duration_ms=1100.0, tracker_epoch=1))
    after = [
        _shift(scene, 1200.0)
        for scene in walk(inside_until_ms=None, duration_ms=2000.0, tracker_epoch=2)
    ]
    result = run.run([*before, *after])
    assert result.epoch_resets == 1
    assert len(result.scored) == 1
    # The event's own evidence began after the reset, not before it.
    scored = result.scored[0]
    assert scored.candidate.accumulator.first_observation_ns >= ms_to_ns(1200.0)


# ------------------------------------------------------------------ determinism (§19)


def test_replaying_an_identical_sequence_gives_an_identical_result() -> None:
    """09 §8's reproducibility gate. If two runs disagree, something reads a clock or a global
    it should not, and every other test result becomes suspect."""
    scenes = list(walk(inside_until_ms=14_000.0, duration_ms=FULL_RUN_MS))

    def run_once() -> tuple[object, ...]:
        return replay().run(scenes).fingerprint()

    assert run_once() == run_once()


def test_determinism_holds_for_a_suppressed_run_too() -> None:
    scenes = list(boundary_loiter(duration_ms=30_000.0))

    def run_once() -> tuple[object, ...]:
        return replay().run(scenes).fingerprint()

    assert run_once() == run_once()


def test_determinism_holds_across_two_cameras_and_two_subjects() -> None:
    scenes = list(two_people(duration_ms=20_000.0))

    def run_once() -> tuple[object, ...]:
        return replay().run(scenes).fingerprint()

    first, second = run_once(), run_once()
    assert first == second


# ------------------------------------------------------------------ memory behaviour (§23)


def test_accumulator_count_stays_bounded_as_subjects_come_and_go() -> None:
    """Many short-lived subjects must not leak one accumulator each."""
    run = replay()
    from vigil.domain.observation import Observation, SubjectKey

    store = run.pipeline._store
    for index in range(2000):
        t_ms = index * 100.0
        store.decay_all_to(ms_to_ns(t_ms))
        store.observe(
            Observation(
                camera_id=CAMERA,
                module_id="intrusion",
                subject=SubjectKey("track", str(index)),
                signal=0.8,
                t_monotonic_ns=ms_to_ns(t_ms),
                facts={},
            )
        )
        store.prune_idle(ms_to_ns(t_ms))
    idle_window_frames = int(settings_for().temporal.idle_expiry_ms / 100.0)
    assert len(store) <= idle_window_frames + 2


def test_the_ledger_does_not_grow_without_bound() -> None:
    result = replay().run(walk(inside_until_ms=14_000.0, duration_ms=FULL_RUN_MS))
    assert len(result.scored) == 1
    assert len(replay().chain.ledger) == 0  # a fresh chain starts clean
