"""Candidate identity, decay driven by the tick, pruning and epoch reset (04 §6.1)."""

from __future__ import annotations

import pytest

from vigil.config.schema.modules import TemporalConfig
from vigil.core.units import ms_to_ns
from vigil.domain.observation import Observation, SubjectKey
from vigil.temporal.store import AccumulatorStore, slot_ms_for

CAMERA = "test-cam"
DEFAULTS = TemporalConfig()
SLOT_MS = 100.0

TRACK_12 = SubjectKey("track", "12")
TRACK_19 = SubjectKey("track", "19")
ZONE_DOCK = SubjectKey("zone", "dock")


def store(config: TemporalConfig = DEFAULTS, *, camera_id: str = CAMERA) -> AccumulatorStore:
    return AccumulatorStore(camera_id=camera_id, config=config, support_slot_ms=SLOT_MS)


def obs(
    subject: SubjectKey,
    t_ms: float,
    signal: float = 0.9,
    *,
    module_id: str = "intrusion",
    camera_id: str = CAMERA,
) -> Observation:
    return Observation(
        camera_id=camera_id,
        module_id=module_id,
        subject=subject,
        signal=signal,
        t_monotonic_ns=ms_to_ns(t_ms),
        facts={},
    )


# ------------------------------------------------------------------ the support slot


def test_the_support_slot_is_derived_from_the_camera_s_inference_rate() -> None:
    assert slot_ms_for(DEFAULTS, 10.0) == pytest.approx(100.0)
    assert slot_ms_for(DEFAULTS, 5.0) == pytest.approx(200.0)


def test_a_configured_support_slot_overrides_the_derived_one() -> None:
    config = DEFAULTS.model_copy(update={"support_slot_ms": 250.0})
    assert slot_ms_for(config, 10.0) == pytest.approx(250.0)


# ------------------------------------------------------------------ identity (§11)


def test_one_persistent_subject_gets_one_accumulator_not_one_per_frame() -> None:
    s = store()
    for i in range(50):
        s.observe(obs(TRACK_12, i * SLOT_MS))
    assert len(s) == 1
    assert s.get("intrusion", TRACK_12) is not None
    assert s.get("intrusion", TRACK_12).state.observation_count == 50


def test_two_subjects_on_one_camera_stay_independent() -> None:
    s = store()
    for i in range(13):
        s.observe(obs(TRACK_12, i * SLOT_MS, 0.9))
        s.observe(obs(TRACK_19, i * SLOT_MS, 0.2))
    assert len(s) == 2
    strong = s.get("intrusion", TRACK_12)
    weak = s.get("intrusion", TRACK_19)
    assert strong is not None and weak is not None
    assert strong.log_odds > 0.0 > weak.log_odds
    assert strong.promotion_ready(ms_to_ns(12 * SLOT_MS))
    assert not weak.promotion_ready(ms_to_ns(12 * SLOT_MS))


def test_two_modules_on_one_subject_stay_independent() -> None:
    s = store()
    s.observe(obs(TRACK_12, 0.0, 0.9, module_id="intrusion"))
    s.observe(obs(TRACK_12, 0.0, 0.1, module_id="loitering"))
    assert len(s) == 2
    assert s.get("intrusion", TRACK_12) is not s.get("loitering", TRACK_12)


def test_subjects_of_different_kinds_never_collide() -> None:
    s = store()
    s.observe(obs(SubjectKey("track", "dock"), 0.0))
    s.observe(obs(ZONE_DOCK, 0.0))
    assert len(s) == 2


def test_separate_cameras_use_separate_stores_and_cannot_merge() -> None:
    """Camera isolation is structural: a store belongs to one camera, so there is no key by
    which two cameras' evidence could meet."""
    cam1, cam2 = store(camera_id="cam-01"), store(camera_id="cam-02")
    for i in range(13):
        cam1.observe(obs(TRACK_12, i * SLOT_MS, 0.9, camera_id="cam-01"))
        cam2.observe(obs(TRACK_12, i * SLOT_MS, 0.2, camera_id="cam-02"))
    a1 = cam1.get("intrusion", TRACK_12)
    a2 = cam2.get("intrusion", TRACK_12)
    assert a1 is not None and a2 is not None
    assert a1.log_odds > 0.0 > a2.log_odds


def test_keys_are_returned_in_a_deterministic_order() -> None:
    s = store()
    for subject in (TRACK_19, ZONE_DOCK, TRACK_12):
        s.observe(obs(subject, 0.0))
    assert s.keys() == (
        ("intrusion", TRACK_12),
        ("intrusion", TRACK_19),
        ("intrusion", ZONE_DOCK),
    )


# ------------------------------------------------------------------ decay driven by the tick


def test_decay_applies_to_accumulators_that_received_nothing_this_tick() -> None:
    """Without a tick-driven decay an abandoned accumulator would freeze at its last value and
    stay influential forever."""
    s = store()
    for i in range(10):
        s.observe(obs(TRACK_12, i * SLOT_MS))
    acc = s.get("intrusion", TRACK_12)
    assert acc is not None
    before = acc.log_odds
    s.decay_all_to(ms_to_ns(900.0 + DEFAULTS.half_life_ms))
    assert acc.log_odds == pytest.approx(before / 2.0, abs=1e-9)


def test_decaying_to_a_time_before_an_accumulator_s_last_update_is_skipped() -> None:
    """Observations from two cameras interleave; a tick must never rewind one of them."""
    s = store()
    s.observe(obs(TRACK_12, 5000.0))
    acc = s.get("intrusion", TRACK_12)
    assert acc is not None
    before = acc.log_odds
    s.decay_all_to(ms_to_ns(1000.0))
    assert acc.log_odds == before


# ------------------------------------------------------------------ pruning


def test_idle_accumulators_are_pruned_and_counted() -> None:
    s = store()
    s.observe(obs(TRACK_12, 0.0))
    s.observe(obs(TRACK_19, 0.0))
    assert s.prune_idle(ms_to_ns(1000.0)) == ()
    pruned = s.prune_idle(ms_to_ns(DEFAULTS.idle_expiry_ms))
    assert pruned == (("intrusion", TRACK_12), ("intrusion", TRACK_19))
    assert len(s) == 0
    assert s.evicted_total == 2


def test_an_active_accumulator_is_never_pruned() -> None:
    s = store()
    for i in range(13):
        s.observe(obs(TRACK_12, i * SLOT_MS))
    acc = s.get("intrusion", TRACK_12)
    assert acc is not None
    acc.mark_active(ms_to_ns(1200.0))
    assert s.prune_idle(ms_to_ns(10 * DEFAULTS.idle_expiry_ms)) == ()
    assert len(s) == 1


def test_memory_stays_bounded_across_many_short_lived_subjects() -> None:
    s = store()
    for i in range(500):
        s.observe(obs(SubjectKey("track", str(i)), i * SLOT_MS))
        s.prune_idle(ms_to_ns(i * SLOT_MS))
    # Only subjects seen inside the idle window survive.
    assert len(s) <= int(DEFAULTS.idle_expiry_ms / SLOT_MS) + 1


# ------------------------------------------------------------------ promotion and release queues


def test_promotion_ready_lists_only_satisfied_accumulators_in_order() -> None:
    s = store()
    for i in range(13):
        s.observe(obs(TRACK_19, i * SLOT_MS, 0.9))
        s.observe(obs(TRACK_12, i * SLOT_MS, 0.9))
        s.observe(obs(ZONE_DOCK, i * SLOT_MS, 0.4))
    ready = s.promotion_ready(ms_to_ns(12 * SLOT_MS))
    assert [a.key[2] for a in ready] == [TRACK_12, TRACK_19]


def test_release_ready_lists_active_accumulators_whose_evidence_has_gone() -> None:
    s = store()
    for i in range(13):
        s.observe(obs(TRACK_12, i * SLOT_MS, 0.9))
    acc = s.get("intrusion", TRACK_12)
    assert acc is not None
    acc.mark_active(ms_to_ns(1200.0))
    assert s.release_ready(ms_to_ns(1200.0)) == []
    t = ms_to_ns(1200.0 + DEFAULTS.half_life_ms * 10 + DEFAULTS.release_sustain_ms)
    s.decay_all_to(t)
    assert [a.key[2] for a in s.release_ready(t)] == [TRACK_12]


# ------------------------------------------------------------------ epoch reset


def test_reset_clears_every_accumulator() -> None:
    """04 §9: after tracker.reset(), no accumulator retains pre-reset evidence. Identities no
    longer mean what they meant, so evidence keyed by them would be attributed to the wrong
    object."""
    s = store()
    for i in range(13):
        s.observe(obs(TRACK_12, i * SLOT_MS))
    assert len(s) == 1
    s.reset()
    assert len(s) == 0
    assert s.get("intrusion", TRACK_12) is None
    assert s.evicted_total == 1


def test_evidence_does_not_survive_a_reset_under_the_same_subject_name() -> None:
    s = store()
    for i in range(13):
        s.observe(obs(TRACK_12, i * SLOT_MS))
    s.reset()
    s.observe(obs(TRACK_12, 1300.0))
    acc = s.get("intrusion", TRACK_12)
    assert acc is not None
    assert acc.state.observation_count == 1
    assert not acc.promotion_ready(ms_to_ns(1300.0))
