"""Severity as explainable arithmetic (06 §8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.support.candidates import CAMERA, candidate, verified
from vigil.config.schema.severity import SeverityBands, SeverityConfig, SeverityWeights, TimeWindow
from vigil.core.geometry import Point, Polygon
from vigil.domain.enums import EventType, Severity, ZoneKind
from vigil.domain.observation import SubjectKey
from vigil.domain.scene import Zone
from vigil.observability.logging import get_logger
from vigil.severity.engine import FACTOR_ORDER, SeverityScorer, band_for, thresholds_in_force
from vigil.severity.factors import (
    FactorInputs,
    factor_base,
    factor_camera_trust,
    factor_confidence,
    factor_escalation,
    factor_object_count,
    factor_persistence,
    factor_time_of_day,
    factor_zone_criticality,
)

CONFIG = SeverityConfig()
NIGHT = datetime(2026, 1, 1, 2, 41, tzinfo=UTC)
AFTERNOON = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)


def zone(zone_id: str = "yard", criticality: int = 3) -> Zone:
    return Zone(
        zone_id=zone_id,
        camera_id=CAMERA,
        kind=ZoneKind.RESTRICTED,
        polygon=Polygon([Point(0.5, 0.4), Point(0.9, 0.4), Point(0.9, 0.9), Point(0.5, 0.9)]),
        criticality=criticality,
        active_schedule=None,
        label=zone_id,
    )


def inputs(**overrides: object) -> FactorInputs:
    base: dict[str, object] = {
        "event": verified(),
        "now_wall_utc": NIGHT,
        "zones": (zone(),),
        "camera_trust_ratio": 1.0,
        "related_event_count": 0,
    }
    return FactorInputs(**{**base, **overrides})  # type: ignore[arg-type]


def scorer(config: SeverityConfig = CONFIG) -> SeverityScorer:
    return SeverityScorer(config=config, logger=get_logger("test.severity"))


# ------------------------------------------------------------------ config invariants


def test_weights_must_sum_to_one() -> None:
    """A config whose weights sum to 1.3 silently saturates every event at CRITICAL."""
    with pytest.raises(ValueError, match=r"must sum to 1\.0"):
        SeverityWeights(base=0.9)


def test_the_shipped_weights_sum_to_one() -> None:
    assert sum(SeverityWeights().model_dump().values()) == pytest.approx(1.0)


def test_bands_must_ascend() -> None:
    with pytest.raises(ValueError, match="strictly ascending"):
        SeverityBands(LOW=50.0, MODERATE=40.0)


def test_an_unknown_event_type_in_base_by_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown event types"):
        SeverityConfig(base_by_type={"not_an_event": 0.5})


def test_every_weighted_factor_has_an_implementation() -> None:
    assert {name for name, _ in FACTOR_ORDER} == set(SeverityWeights().model_dump())


# ------------------------------------------------------------------ individual factors


def test_base_comes_from_the_event_type() -> None:
    """Fire is not an equal of an abandoned bag."""
    assert factor_base(inputs(), CONFIG) == pytest.approx(0.65)  # intrusion
    fire = verified(cand=candidate(event_type=EventType.FIRE))
    assert factor_base(inputs(event=fire), CONFIG) == pytest.approx(1.0)
    bag = verified(cand=candidate(event_type=EventType.ABANDONED_OBJECT))
    assert factor_base(inputs(event=bag), CONFIG) == pytest.approx(0.45)


def test_an_unlisted_event_type_falls_back_to_the_default() -> None:
    config = SeverityConfig(base_by_type={})
    assert factor_base(inputs(), config) == pytest.approx(config.base_default)


def test_zone_criticality_uses_the_most_critical_involved_zone() -> None:
    assert factor_zone_criticality(inputs(zones=(zone(criticality=3),)), CONFIG) == pytest.approx(
        0.75
    )
    assert factor_zone_criticality(inputs(zones=(zone(criticality=4),)), CONFIG) == pytest.approx(
        1.0
    )
    both = (zone("yard", 1), zone("other", 4))
    # Only "yard" is involved in the candidate, so the uninvolved critical zone must not count.
    assert factor_zone_criticality(inputs(zones=both), CONFIG) == pytest.approx(0.25)


def test_a_zone_nobody_rated_contributes_nothing() -> None:
    assert factor_zone_criticality(inputs(zones=()), CONFIG) == 0.0


def test_confidence_is_the_post_verification_value() -> None:
    assert factor_confidence(inputs(event=verified(confidence=0.42)), CONFIG) == pytest.approx(0.42)


def test_object_count_saturates() -> None:
    """Fifty people is not ten times worse than five."""
    config = SeverityConfig(count_saturation=5.0)
    one = candidate(subject=SubjectKey("track", "1"))
    assert factor_object_count(inputs(event=verified(cand=one)), config) == pytest.approx(0.2)
    many = SeverityConfig(count_saturation=1.0)
    assert factor_object_count(inputs(event=verified(cand=one)), many) == pytest.approx(1.0)


def test_persistence_saturates_over_the_configured_window() -> None:
    config = SeverityConfig(persistence_saturation_ms=60_000.0)
    # The default candidate accumulated over 1200 ms.
    assert factor_persistence(inputs(), config) == pytest.approx(0.02, abs=0.001)
    brief = SeverityConfig(persistence_saturation_ms=1000.0)
    assert factor_persistence(inputs(), brief) == pytest.approx(1.0)


def test_time_of_day_follows_the_configured_policy() -> None:
    """An intrusion at 03:00 outranks one at 14:00."""
    assert factor_time_of_day(inputs(now_wall_utc=NIGHT), CONFIG) == pytest.approx(1.0)
    assert factor_time_of_day(inputs(now_wall_utc=AFTERNOON), CONFIG) == pytest.approx(0.3)


def test_the_time_policy_is_configurable_not_assumed() -> None:
    config = SeverityConfig(
        time_policy=(TimeWindow(start="09:00", end="17:00", weight_ratio=1.0),),
        time_default_weight_ratio=0.1,
    )
    assert factor_time_of_day(inputs(now_wall_utc=AFTERNOON), config) == pytest.approx(1.0)
    assert factor_time_of_day(inputs(now_wall_utc=NIGHT), config) == pytest.approx(0.1)


def test_a_time_window_may_wrap_midnight() -> None:
    window = TimeWindow(start="22:00", end="06:00", weight_ratio=1.0)
    assert window.covers(23 * 60)
    assert window.covers(2 * 60)
    assert not window.covers(12 * 60)


def test_escalation_saturates_in_related_events() -> None:
    config = SeverityConfig(escalation_saturation=5.0)
    assert factor_escalation(inputs(related_event_count=0), config) == 0.0
    assert factor_escalation(inputs(related_event_count=2), config) == pytest.approx(0.4)
    assert factor_escalation(inputs(related_event_count=50), config) == pytest.approx(1.0)


def test_camera_trust_lowers_a_camera_that_cries_wolf() -> None:
    assert factor_camera_trust(inputs(camera_trust_ratio=1.0), CONFIG) == pytest.approx(1.0)
    assert factor_camera_trust(inputs(camera_trust_ratio=0.3), CONFIG) == pytest.approx(0.3)


@pytest.mark.parametrize("factor", [f for _, f in FACTOR_ORDER])
def test_every_factor_returns_a_ratio(factor: object) -> None:
    value = factor(inputs(), CONFIG)  # type: ignore[operator]
    assert 0.0 <= value <= 1.0


# ------------------------------------------------------------------ bands


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, Severity.INFO),
        (19.9, Severity.INFO),
        (20.0, Severity.LOW),
        (39.9, Severity.LOW),
        (40.0, Severity.MODERATE),
        (64.9, Severity.MODERATE),
        (65.0, Severity.HIGH),
        (84.9, Severity.HIGH),
        (85.0, Severity.CRITICAL),
        (100.0, Severity.CRITICAL),
    ],
)
def test_the_score_maps_to_a_band(score: float, expected: Severity) -> None:
    assert band_for(score, CONFIG) is expected


def test_bands_are_configurable() -> None:
    strict = SeverityConfig(bands=SeverityBands(LOW=10.0, MODERATE=20.0, HIGH=30.0, CRITICAL=40.0))
    assert band_for(35.0, strict) is Severity.HIGH
    assert band_for(35.0, CONFIG) is Severity.LOW


# ------------------------------------------------------------------ scoring


def test_the_factors_sum_to_the_score() -> None:
    """Severity is explainable as arithmetic: if an operator disagrees they can see which
    factor to argue with (06 §8)."""
    assessment = scorer().score(inputs())
    total = sum(f.contribution for f in assessment.factors)
    assert assessment.score == pytest.approx(100.0 * total, abs=1e-9)


def test_every_factor_is_recorded_with_its_raw_value_and_weight() -> None:
    assessment = scorer().score(inputs())
    assert len(assessment.factors) == 8
    weights = SeverityWeights().model_dump()
    for factor in assessment.factors:
        assert factor.weight == pytest.approx(weights[factor.name])
        assert factor.contribution == pytest.approx(factor.raw_value * factor.weight)


def test_the_worked_example_from_the_architecture_reproduces_its_factors() -> None:
    """06 §12: a person in a criticality-3 restricted yard at 02:41, confidence 0.92, one track,
    1200 ms of evidence, no escalation, full trust.

    Every factor below is the value the document states. They sum to **60.5**, not the 61.9 the
    document's headline reports, and 60.5 falls in MODERATE because `severity.bands.HIGH` is 65.
    The document's narrative is wrong on both counts; its factor table and weights — which are
    the normative part — are what this engine implements. Recorded here rather than "fixed" by
    moving a band, because tuning thresholds to make a typo come true would be the worst of
    both worlds.
    """
    assessment = scorer().score(
        inputs(event=verified(confidence=0.92), now_wall_utc=NIGHT, zones=(zone(criticality=3),))
    )
    by_name = {f.name: f for f in assessment.factors}
    assert by_name["base"].raw_value == pytest.approx(0.65)
    assert by_name["zone_criticality"].raw_value == pytest.approx(0.75)
    assert by_name["confidence"].raw_value == pytest.approx(0.92)
    assert by_name["object_count"].raw_value == pytest.approx(0.2)
    assert by_name["persistence"].raw_value == pytest.approx(0.02, abs=0.001)
    assert by_name["time_of_day"].raw_value == pytest.approx(1.0)
    assert by_name["escalation"].raw_value == pytest.approx(0.0)
    assert by_name["camera_trust"].raw_value == pytest.approx(1.0)
    assert assessment.score == pytest.approx(60.5, abs=0.1)
    assert assessment.severity is Severity.MODERATE


def test_the_same_event_in_daylight_scores_lower() -> None:
    night = scorer().score(inputs(now_wall_utc=NIGHT))
    day = scorer().score(inputs(now_wall_utc=AFTERNOON))
    assert day.score < night.score


def test_scoring_is_deterministic() -> None:
    assert scorer().score(inputs()) == scorer().score(inputs())


def test_a_factor_that_fails_degrades_the_assessment_instead_of_losing_it() -> None:
    """Severity fails *open*: an event we cannot fully score is still worth showing (01 §6)."""
    broken = SeverityConfig(count_saturation=1.0)
    bad_inputs = inputs(event=None)  # every factor that touches the event will raise
    assessment = scorer(broken).score(bad_inputs)
    assert assessment.degraded
    assert 0.0 <= assessment.score <= 100.0


def test_a_healthy_assessment_is_not_marked_degraded() -> None:
    assert not scorer().score(inputs()).degraded


def test_severity_never_changes_the_event_s_confidence() -> None:
    """They answer different questions; coupling them would make a dangerous event look more
    certain merely because it is dangerous (06 §8)."""
    event = verified(confidence=0.5)
    before = event.confidence_ratio
    scorer().score(inputs(event=event))
    assert event.confidence_ratio == before


def test_the_explanation_reads_as_arithmetic() -> None:
    assessment = scorer().score(inputs())
    text = scorer().explain(assessment)
    assert "base" in text and "->" in text
    assert f"{assessment.score:.1f}" in text


def test_thresholds_in_force_are_captured_for_the_explanation() -> None:
    """Captured per event, not read from current config at display time: thresholds change as
    the system is tuned, and an explanation rendered against today's misrepresents a decision
    made under yesterday's (05 §4)."""
    thresholds = thresholds_in_force(CONFIG)
    assert thresholds["severity.bands.HIGH"] == pytest.approx(65.0)
    assert thresholds["severity.weights.base"] == pytest.approx(0.30)
    assert thresholds["severity.persistence_saturation_ms"] == pytest.approx(60_000.0)


def test_a_zero_weighted_factor_still_appears_in_the_breakdown() -> None:
    """Dropping it would make the breakdown stop summing to the score."""
    config = SeverityConfig(
        weights=SeverityWeights(
            base=0.5,
            zone_criticality=0.5,
            confidence=0.0,
            object_count=0.0,
            persistence=0.0,
            time_of_day=0.0,
            escalation=0.0,
            camera_trust=0.0,
        )
    )
    assessment = scorer(config).score(inputs())
    assert len(assessment.factors) == 8
    assert sum(f.contribution for f in assessment.factors) * 100.0 == pytest.approx(
        assessment.score
    )
