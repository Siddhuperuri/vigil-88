"""The temporal mechanism of 04 §6, including every property test named in 04 §9.

Every test drives time explicitly. Nothing sleeps, nothing reads a clock, and a ten-minute
scenario runs in microseconds (09 §1).
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from vigil.config.schema.modules import TemporalConfig
from vigil.core.errors import TemporalError
from vigil.core.units import ms_to_ns
from vigil.domain.observation import Observation, SubjectKey
from vigil.temporal.accumulator import (
    NEUTRAL_SIGNAL,
    Accumulator,
    confidence_of,
    decayed,
    log_odds_of,
)

CAMERA = "test-cam"
MODULE = "intrusion"
SUBJECT = SubjectKey("track", "42")
DEFAULTS = TemporalConfig()
SLOT_MS = 100.0


def make(config: TemporalConfig = DEFAULTS, *, slot_ms: float = SLOT_MS) -> Accumulator:
    return Accumulator(
        camera_id=CAMERA, module_id=MODULE, subject=SUBJECT, config=config, support_slot_ms=slot_ms
    )


def obs(t_ms: float, signal: float, *, subject: SubjectKey = SUBJECT) -> Observation:
    return Observation(
        camera_id=CAMERA,
        module_id=MODULE,
        subject=subject,
        signal=signal,
        t_monotonic_ns=ms_to_ns(t_ms),
        facts={},
    )


def feed(acc: Accumulator, signals: list[float], *, step_ms: float = SLOT_MS) -> None:
    for i, s in enumerate(signals):
        acc.observe(obs(i * step_ms, s))


# ------------------------------------------------------------------ the update rule, step by step


def test_an_accumulator_starts_at_even_odds_with_no_evidence() -> None:
    acc = make()
    assert acc.log_odds == 0.0
    assert acc.state.confidence_ratio == pytest.approx(0.5)
    assert acc.state.observation_count == 0
    assert not acc.is_active


def test_log_odds_of_a_signal_is_the_logit() -> None:
    assert log_odds_of(0.5, signal_floor=0.02) == 0.0
    assert log_odds_of(0.75, signal_floor=0.02) == pytest.approx(math.log(3.0))
    assert log_odds_of(0.25, signal_floor=0.02) == pytest.approx(-math.log(3.0))


def test_the_signal_floor_bounds_one_frame_s_contribution() -> None:
    """Without the clamp a detector reporting 1.0 would add infinite, unfalsifiable evidence."""
    floor = 0.02
    at_one = log_odds_of(1.0, signal_floor=floor)
    at_clamp = log_odds_of(1.0 - floor, signal_floor=floor)
    assert math.isfinite(at_one)
    assert at_one == pytest.approx(at_clamp)
    assert log_odds_of(0.0, signal_floor=floor) == pytest.approx(-at_clamp)


def test_confidence_is_the_logistic_of_log_odds() -> None:
    assert confidence_of(0.0, confidence_scale=1.0) == pytest.approx(0.5)
    assert confidence_of(2.2, confidence_scale=1.0) == pytest.approx(0.9002495, abs=1e-6)
    assert confidence_of(-2.2, confidence_scale=1.0) == pytest.approx(0.0997505, abs=1e-6)


def test_confidence_saturates_instead_of_overflowing() -> None:
    assert confidence_of(1e6, confidence_scale=1.0) == 1.0
    assert confidence_of(-1e6, confidence_scale=1.0) == 0.0


# ------------------------------------------------------------------ signal semantics (§7)


def test_a_neutral_signal_contributes_exactly_zero() -> None:
    acc = make()
    acc.observe(obs(0.0, NEUTRAL_SIGNAL))
    assert acc.log_odds == 0.0


def test_two_hundred_neutral_observations_never_confirm() -> None:
    """The property worth the most: being unsure for a long time is not evidence."""
    acc = make()
    for i in range(200):
        acc.observe(obs(i * SLOT_MS, NEUTRAL_SIGNAL))
    assert acc.log_odds == pytest.approx(0.0, abs=1e-12)
    assert not acc.promotion_ready(ms_to_ns(200 * SLOT_MS))


@pytest.mark.parametrize(
    ("signal", "direction"),
    [
        (0.95, "strong positive"),
        (0.6, "weak positive"),
        (0.4, "weak negative"),
        (0.05, "strong negative"),
    ],
)
def test_evidence_moves_log_odds_in_the_signal_s_direction(signal: float, direction: str) -> None:
    acc = make()
    acc.observe(obs(0.0, signal))
    assert (acc.log_odds > 0.0) is (signal > NEUTRAL_SIGNAL), direction


def test_negative_evidence_undoes_positive_evidence() -> None:
    acc = make()
    acc.observe(obs(0.0, 0.8))
    positive = acc.log_odds
    acc.observe(obs(0.0, 0.2))  # the exact mirror signal, no time passing
    assert positive > 0.0
    assert acc.log_odds == pytest.approx(0.0)


# ------------------------------------------------------------------ half-life decay (§8)


def test_decay_is_exact_over_one_half_life() -> None:
    """04 §9: one observation, advance one half-life, assert L halved to within 1e-9."""
    acc = make()
    acc.observe(obs(0.0, 0.9))
    before = acc.log_odds
    acc.decay_to(ms_to_ns(DEFAULTS.half_life_ms))
    assert acc.log_odds == pytest.approx(before / 2.0, abs=1e-9)


@pytest.mark.parametrize("half_lives", [1, 2, 3, 10])
def test_decay_compounds_over_several_half_lives(half_lives: int) -> None:
    acc = make()
    acc.observe(obs(0.0, 0.9))
    before = acc.log_odds
    acc.decay_to(ms_to_ns(DEFAULTS.half_life_ms * half_lives))
    assert acc.log_odds == pytest.approx(before * 2.0**-half_lives, abs=1e-9)


@pytest.mark.parametrize("half_life_ms", [250.0, 1000.0, 2000.0, 30_000.0])
def test_a_configured_half_life_is_honoured(half_life_ms: float) -> None:
    config = DEFAULTS.model_copy(update={"half_life_ms": half_life_ms})
    acc = make(config)
    acc.observe(obs(0.0, 0.9))
    before = acc.log_odds
    acc.decay_to(ms_to_ns(half_life_ms))
    assert acc.log_odds == pytest.approx(before / 2.0, abs=1e-9)


def test_a_zero_length_step_changes_nothing() -> None:
    assert decayed(3.0, dt_ms=0.0, half_life_ms=2000.0) == 3.0
    assert decayed(3.0, dt_ms=-5.0, half_life_ms=2000.0) == 3.0


def test_a_long_gap_decays_evidence_toward_irrelevance() -> None:
    acc = make()
    feed(acc, [0.9] * 10)
    strong = acc.log_odds
    acc.decay_to(ms_to_ns(60_000))
    assert strong > DEFAULTS.l_activate
    assert abs(acc.log_odds) < 1e-6


def test_intermittent_evidence_accumulates_more_slowly_than_continuous() -> None:
    continuous = make()
    feed(continuous, [0.8] * 10, step_ms=100.0)
    intermittent = make()
    feed(intermittent, [0.8] * 10, step_ms=1000.0)
    assert continuous.log_odds > intermittent.log_odds


def test_stale_evidence_does_not_stay_influential() -> None:
    acc = make()
    feed(acc, [0.95] * 20)
    assert acc.log_odds == pytest.approx(DEFAULTS.l_bound)
    # Ten half-lives later a single weak-negative observation should dominate what is left.
    t = ms_to_ns(20 * SLOT_MS + DEFAULTS.half_life_ms * 10)
    acc.decay_to(t)
    assert acc.log_odds < 0.01


# ------------------------------------------------------------------ bounding


def test_log_odds_is_bounded_above_and_below() -> None:
    high = make()
    feed(high, [1.0] * 200)
    low = make()
    feed(low, [0.0] * 200)
    assert high.log_odds == pytest.approx(DEFAULTS.l_bound)
    assert low.log_odds == pytest.approx(-DEFAULTS.l_bound)


def test_recovery_from_saturation_is_bounded() -> None:
    """Why the bound exists: a saturated accumulator must be falsifiable in finite time."""
    acc = make()
    feed(acc, [1.0] * 100)
    acc.decay_to(ms_to_ns(100 * SLOT_MS + DEFAULTS.half_life_ms * 20))
    assert acc.log_odds < DEFAULTS.l_release


# ------------------------------------------------------------------ promotion (§6.3)


def test_continuous_strong_evidence_satisfies_every_promotion_condition() -> None:
    acc = make()
    feed(acc, [0.7] * 13)  # 1200 ms at 10 fps
    now = ms_to_ns(12 * SLOT_MS)
    state = acc.state
    assert state.log_odds >= DEFAULTS.l_activate
    assert state.observation_count >= DEFAULTS.min_observations
    assert state.support_ratio >= DEFAULTS.min_support_ratio
    assert acc.promotion_ready(now)


def test_min_duration_blocks_a_burst_that_is_strong_but_brief() -> None:
    acc = make()
    feed(acc, [0.95] * 5, step_ms=10.0)  # five frames in 40 ms
    now = ms_to_ns(40.0)
    assert acc.log_odds >= DEFAULTS.l_activate
    assert acc.state.observation_count >= DEFAULTS.min_observations
    assert not acc.promotion_ready(now)  # elapsed 40 ms < min_duration_ms 1200


def test_min_observations_blocks_a_single_overwhelming_frame() -> None:
    config = DEFAULTS.model_copy(update={"min_duration_ms": 0})
    acc = make(config)
    acc.observe(obs(0.0, 1.0))
    assert acc.log_odds >= config.l_activate
    assert not acc.promotion_ready(0)


def test_min_support_ratio_blocks_a_flickering_detector() -> None:
    """04 §9: four strong frames spread over fifteen seconds must not promote."""
    config = DEFAULTS.model_copy(update={"half_life_ms": 1e9})  # remove decay; isolate support
    acc = make(config)
    for i in range(4):
        acc.observe(obs(i * 4000.0, 0.95))
    now = ms_to_ns(15_000)
    acc.decay_to(now)
    assert acc.log_odds >= config.l_activate
    assert acc.state.observation_count >= config.min_observations
    assert acc.support_ratio(now) < config.min_support_ratio
    assert not acc.promotion_ready(now)


def test_support_ratio_is_one_for_unbroken_evidence() -> None:
    acc = make()
    feed(acc, [0.8] * 20)
    assert acc.support_ratio(ms_to_ns(19 * SLOT_MS)) == pytest.approx(1.0)


def test_neutral_observations_do_not_count_as_support() -> None:
    acc = make()
    feed(acc, [NEUTRAL_SIGNAL] * 20)
    assert acc.support_ratio(ms_to_ns(19 * SLOT_MS)) == 0.0


def test_an_already_active_accumulator_does_not_promote_again() -> None:
    acc = make()
    feed(acc, [0.7] * 13)
    now = ms_to_ns(12 * SLOT_MS)
    assert acc.promotion_ready(now)
    acc.mark_active(now)
    assert not acc.promotion_ready(now)


def test_marking_an_active_accumulator_active_again_is_rejected() -> None:
    acc = make()
    feed(acc, [0.7] * 13)
    acc.mark_active(ms_to_ns(1200))
    with pytest.raises(TemporalError, match="already active"):
        acc.mark_active(ms_to_ns(1300))


# ------------------------------------------------------------------ hysteresis (§9)


def test_release_requires_l_release_sustained() -> None:
    acc = make()
    feed(acc, [0.9] * 13)
    acc.mark_active(ms_to_ns(1200))
    # Decay until L is at or below l_release, and check release is not immediate.
    t = 1200.0
    while acc.log_odds > DEFAULTS.l_release:
        t += 100.0
        acc.decay_to(ms_to_ns(t))
    assert not acc.release_ready(ms_to_ns(t))
    assert acc.release_ready(ms_to_ns(t + DEFAULTS.release_sustain_ms))


def test_evidence_returning_before_the_sustain_window_cancels_release() -> None:
    """The one non-monotonic edge (06 §9): RESOLVING returns to ACTIVE on new evidence."""
    acc = make()
    feed(acc, [0.9] * 13)
    acc.mark_active(ms_to_ns(1200))
    t = 1200.0
    while acc.log_odds > DEFAULTS.l_release:
        t += 100.0
        acc.decay_to(ms_to_ns(t))
    acc.observe(obs(t + 500.0, 0.95))  # support returns
    assert not acc.release_ready(ms_to_ns(t + 500.0 + DEFAULTS.release_sustain_ms * 2))


def test_an_accumulator_still_above_l_release_remains_confirmed() -> None:
    """The hysteresis gap: between l_release and l_activate nothing changes state."""
    acc = make()
    feed(acc, [0.9] * 13)
    acc.mark_active(ms_to_ns(1200))
    t = 1200.0
    while acc.log_odds > (DEFAULTS.l_activate + DEFAULTS.l_release) / 2.0:
        t += 100.0
        acc.decay_to(ms_to_ns(t))
    assert DEFAULTS.l_release < acc.log_odds < DEFAULTS.l_activate
    assert acc.is_active
    assert not acc.release_ready(ms_to_ns(t + 10 * DEFAULTS.release_sustain_ms))


def test_a_signal_oscillating_across_the_boundary_does_not_flap() -> None:
    """04 §9: oscillation for 60 s yields at most one confirm and one release."""
    acc = make()
    confirms = 0
    releases = 0
    t = 0.0
    for i in range(600):  # 60 s at 10 fps
        t = i * 100.0
        acc.observe(obs(t, 0.75 if i % 2 == 0 else 0.3))
        if acc.promotion_ready(ms_to_ns(t)):
            acc.mark_active(ms_to_ns(t))
            confirms += 1
        elif acc.release_ready(ms_to_ns(t)):
            acc.mark_released()
            releases += 1
    assert confirms <= 1
    assert releases <= 1


def test_release_timing_does_not_depend_on_how_often_the_engine_looks() -> None:
    """The same evidence resolved at 10 fps and in one long step must release at the same time.

    Dating the drop below `l_release` to the tick that noticed it would make a camera analysed
    at 2 fps resolve later than one at 10 fps, for no reason an operator could explain.
    """

    def released_at(tick_ms: float) -> float:
        acc = make()
        feed(acc, [0.9] * 13)
        acc.mark_active(ms_to_ns(1200.0))
        t = 1200.0
        while not acc.release_ready(ms_to_ns(t)):
            t += tick_ms
            acc.decay_to(ms_to_ns(t))
            if t > 300_000.0:
                pytest.fail("never released")
        return t

    fine, coarse = released_at(100.0), released_at(5000.0)
    assert fine == pytest.approx(coarse, abs=5000.0)


def test_release_is_not_satisfied_by_a_single_enormous_time_step_alone() -> None:
    """The sustain window must have genuinely elapsed, not merely be spanned by one step."""
    acc = make()
    feed(acc, [0.9] * 13)
    acc.mark_active(ms_to_ns(1200.0))
    # L saturates at l_bound, so reaching l_release takes log2(8.0/0.8) ~ 3.32 half-lives.
    # Step just past that crossing: the sustain window has not run yet.
    just_past_ms = 1200.0 + DEFAULTS.half_life_ms * 3.5
    acc.decay_to(ms_to_ns(just_past_ms))
    assert acc.log_odds <= DEFAULTS.l_release
    assert not acc.release_ready(ms_to_ns(just_past_ms))
    assert acc.release_ready(ms_to_ns(just_past_ms + DEFAULTS.release_sustain_ms))


def test_releasing_ends_the_episode_and_starts_a_fresh_window() -> None:
    """A recurrence is a new event with its own duration and evidence (06 §9), so the old
    window must not let the next one satisfy min_duration_ms instantly."""
    acc = make()
    feed(acc, [0.9] * 13)
    acc.mark_active(ms_to_ns(1200.0))
    acc.mark_released()
    assert acc.state.observation_count == 0
    assert acc.first_observation_ns is None
    acc.observe(obs(30_000.0, 0.95))
    assert not acc.promotion_ready(ms_to_ns(30_000.0))


def test_released_accumulators_can_be_promoted_again_by_fresh_evidence() -> None:
    acc = make()
    feed(acc, [0.9] * 13)
    acc.mark_active(ms_to_ns(1200))
    acc.mark_released()
    assert not acc.is_active
    for i in range(13):
        acc.observe(obs(20_000.0 + i * 100.0, 0.9))
    assert acc.promotion_ready(ms_to_ns(21_200))


# ------------------------------------------------------------------ failure handling (§22)


def test_an_observation_for_another_subject_is_rejected() -> None:
    acc = make()
    with pytest.raises(TemporalError, match="does not belong"):
        acc.observe(obs(0.0, 0.9, subject=SubjectKey("track", "99")))


def test_a_backwards_timestamp_is_rejected_and_changes_nothing() -> None:
    acc = make()
    acc.observe(obs(1000.0, 0.9))
    before = acc.log_odds
    with pytest.raises(TemporalError, match="precedes"):
        acc.observe(obs(500.0, 0.9))
    assert acc.log_odds == before
    assert acc.state.observation_count == 1


def test_decaying_backwards_is_rejected() -> None:
    acc = make()
    acc.observe(obs(1000.0, 0.9))
    with pytest.raises(TemporalError, match="before the last update"):
        acc.decay_to(ms_to_ns(500.0))


def test_a_non_positive_support_slot_is_rejected_at_construction() -> None:
    with pytest.raises(TemporalError, match="support_slot_ms must be positive"):
        make(slot_ms=0.0)


def test_two_observations_with_the_same_timestamp_both_count() -> None:
    """A duplicate is not an error: two modules may legitimately observe one frame, and the
    engine must not silently drop evidence. Decay over a zero step is a no-op."""
    acc = make()
    acc.observe(obs(0.0, 0.8))
    once = acc.log_odds
    acc.observe(obs(0.0, 0.8))
    assert acc.log_odds == pytest.approx(2.0 * once)
    assert acc.state.observation_count == 2


def test_the_observation_window_is_bounded() -> None:
    config = DEFAULTS.model_copy(update={"max_window_observations": 8})
    acc = make(config)
    feed(acc, [0.9] * 50)
    assert len(acc.window()) == 8
    assert acc.state.observation_count == 50  # the count is not forgotten, the samples are


# ------------------------------------------------------------------ idle expiry


def test_an_inactive_accumulator_goes_idle_after_the_expiry_window() -> None:
    acc = make()
    acc.observe(obs(0.0, 0.9))
    assert not acc.is_idle_at(ms_to_ns(DEFAULTS.idle_expiry_ms - 1))
    assert acc.is_idle_at(ms_to_ns(DEFAULTS.idle_expiry_ms))


def test_an_active_accumulator_never_goes_idle() -> None:
    acc = make()
    feed(acc, [0.9] * 13)
    acc.mark_active(ms_to_ns(1200))
    assert not acc.is_idle_at(ms_to_ns(10 * DEFAULTS.idle_expiry_ms))


# ------------------------------------------------------------------ properties (04 §9, 09 §6)

SIGNALS = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
GAPS_MS = st.integers(min_value=1, max_value=5000)


@given(
    signals=st.lists(SIGNALS, min_size=1, max_size=200),
    gaps_ms=st.lists(GAPS_MS, min_size=1, max_size=200),
)
@settings(max_examples=200, deadline=None)
def test_property_log_odds_stays_bounded(signals: list[float], gaps_ms: list[int]) -> None:
    acc = make()
    t = 0.0
    for s, gap in zip(signals, gaps_ms, strict=False):
        t += gap
        state = acc.observe(obs(t, s))
        assert -DEFAULTS.l_bound <= state.log_odds <= DEFAULTS.l_bound


@given(signals=st.lists(SIGNALS, min_size=5, max_size=100))
@settings(max_examples=200, deadline=None)
def test_property_stronger_evidence_never_lowers_log_odds(signals: list[float]) -> None:
    def final(values: list[float]) -> float:
        acc = make()
        feed(acc, values)
        return acc.log_odds

    weaker = [min(s, NEUTRAL_SIGNAL) for s in signals]
    assert final(signals) >= final(weaker) - 1e-9


@given(n=st.integers(min_value=1, max_value=500), gap_ms=GAPS_MS)
@settings(max_examples=100, deadline=None)
def test_property_neutral_signal_never_accumulates(n: int, gap_ms: int) -> None:
    acc = make()
    for i in range(n):
        acc.observe(obs(i * gap_ms, NEUTRAL_SIGNAL))
    assert abs(acc.log_odds) < 1e-9
    assert not acc.promotion_ready(ms_to_ns(n * gap_ms))


@given(
    signals=st.lists(SIGNALS, min_size=1, max_size=60),
    gaps_ms=st.lists(GAPS_MS, min_size=1, max_size=60),
)
@settings(max_examples=150, deadline=None)
def test_property_absence_of_evidence_decays_confidence(
    signals: list[float], gaps_ms: list[int]
) -> None:
    acc = make()
    t = 0.0
    for s, gap in zip(signals, gaps_ms, strict=False):
        t += gap
        acc.observe(obs(t, s))
    assume(abs(acc.log_odds) > 1e-6)
    magnitude = abs(acc.log_odds)
    acc.decay_to(ms_to_ns(t + DEFAULTS.half_life_ms))
    assert abs(acc.log_odds) < magnitude


@given(
    signals=st.lists(SIGNALS, min_size=1, max_size=80),
    gaps_ms=st.lists(GAPS_MS, min_size=1, max_size=80),
)
@settings(max_examples=150, deadline=None)
def test_property_the_same_sequence_gives_the_same_state(
    signals: list[float], gaps_ms: list[int]
) -> None:
    """Replay determinism at the smallest scale: no hidden clock, no global, no randomness."""

    def run() -> tuple[float, int, float, bool]:
        acc = make()
        t = 0.0
        for s, gap in zip(signals, gaps_ms, strict=False):
            t += gap
            acc.observe(obs(t, s))
        state = acc.state_at(ms_to_ns(t))
        return (state.log_odds, state.observation_count, state.support_ratio, state.is_active)

    assert run() == run()


@given(
    signal=SIGNALS,
    half_life_ms=st.floats(min_value=1.0, max_value=60_000.0),
    dt_ms=st.floats(min_value=0.0, max_value=600_000.0),
)
@settings(max_examples=200, deadline=None)
def test_property_decay_never_changes_sign_or_grows(
    signal: float, half_life_ms: float, dt_ms: float
) -> None:
    before = log_odds_of(signal, signal_floor=DEFAULTS.signal_floor)
    after = decayed(before, dt_ms=dt_ms, half_life_ms=half_life_ms)
    assert abs(after) <= abs(before) + 1e-12
    assert after == 0.0 or (after > 0.0) == (before > 0.0)


@given(
    log_odds=st.floats(min_value=-20.0, max_value=20.0),
    scale=st.floats(min_value=0.1, max_value=10.0),
)
@settings(max_examples=200, deadline=None)
def test_property_confidence_is_a_monotonic_map_into_the_unit_interval(
    log_odds: float, scale: float
) -> None:
    c = confidence_of(log_odds, confidence_scale=scale)
    assert 0.0 <= c <= 1.0
    assert confidence_of(log_odds + 1.0, confidence_scale=scale) >= c
