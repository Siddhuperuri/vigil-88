"""The chain's contract: first REJECT wins, DEFER holds, every verdict is kept (06 §6)."""

from __future__ import annotations

import pytest

from tests.support.candidates import CAMERA, accumulate, candidate, state, unhealthy
from tests.support.scenes import SceneStateBuilder
from vigil.config.schema.modules import VerificationConfig
from vigil.core.capabilities import Availability
from vigil.domain.enums import CameraState, CandidateOutcome, ValidatorOutcome, ZoneKind
from vigil.domain.event import CandidateEvent
from vigil.domain.observation import SubjectKey
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.verification.engine import DEFERRAL_EXPIRED, VerificationEngine
from vigil.verification.ledger import SuppressionLedger, dedup_key
from vigil.verification.validators.context import (
    VerificationState,
    deferred,
    passed,
    rejected,
)


@pytest.fixture
def metrics() -> MetricsRegistry:
    return MetricsRegistry()


def engine(
    metrics: MetricsRegistry,
    *,
    chain: tuple[object, ...] | None = None,
    config: VerificationConfig | None = None,
    ledger: SuppressionLedger | None = None,
) -> VerificationEngine:
    resolved = config or VerificationConfig()
    return VerificationEngine(
        ledger=ledger if ledger is not None else SuppressionLedger(resolved),
        config=resolved,
        logger=get_logger("test.verify"),
        metrics=metrics,
        chain=chain if chain is not None else (),  # type: ignore[arg-type]
    )


def always_pass(name: str = "ok"):  # type: ignore[no-untyped-def]
    def validator(c: CandidateEvent, s: VerificationState):  # type: ignore[no-untyped-def]
        return passed(name, "no objection")

    validator.__name__ = f"validate_{name}"
    return validator


def always_reject(name: str = "nope", reason: str = "because"):  # type: ignore[no-untyped-def]
    def validator(c: CandidateEvent, s: VerificationState):  # type: ignore[no-untyped-def]
        return rejected(name, reason)

    validator.__name__ = f"validate_{name}"
    return validator


def always_defer(name: str = "wait", reason: str = "needs more history"):  # type: ignore[no-untyped-def]
    def validator(c: CandidateEvent, s: VerificationState):  # type: ignore[no-untyped-def]
        return deferred(name, reason)

    validator.__name__ = f"validate_{name}"
    return validator


def always_raise(name: str = "broken"):  # type: ignore[no-untyped-def]
    def validator(c: CandidateEvent, s: VerificationState):  # type: ignore[no-untyped-def]
        raise RuntimeError("validator exploded")

    validator.__name__ = f"validate_{name}"
    return validator


# ------------------------------------------------------------------ acceptance


def test_a_candidate_that_survives_every_validator_is_verified(metrics: MetricsRegistry) -> None:
    eng = engine(metrics, chain=(always_pass("a"), always_pass("b")))
    result = eng.verify(candidate(), state())
    assert result.outcome is CandidateOutcome.VERIFIED
    assert result.event is not None
    assert [v.validator_id for v in result.verdicts] == ["a", "b"]


def test_all_verdicts_are_kept_including_passes(metrics: MetricsRegistry) -> None:
    eng = engine(metrics, chain=(always_pass("a"), always_pass("b"), always_pass("c")))
    result = eng.verify(candidate(), state())
    assert len(result.verdicts) == 3
    assert result.event is not None
    assert len(result.event.verdicts) == 3


def test_verification_opens_the_event_in_the_ledger(metrics: MetricsRegistry) -> None:
    config = VerificationConfig()
    ledger = SuppressionLedger(config)
    eng = engine(metrics, chain=(always_pass(),), config=config, ledger=ledger)
    cand = candidate()
    eng.verify(cand, state(ledger=ledger, config=config))
    key = dedup_key(cand.camera_id, cand.event_type, cand.subject)
    assert ledger.is_open(key)


def test_confidence_comes_from_accumulation_not_verification(metrics: MetricsRegistry) -> None:
    """PASS means "no objection", never "I am confident" (06 §6)."""
    eng = engine(metrics, chain=(always_pass(), always_pass(), always_pass()))
    cand = candidate()
    result = eng.verify(cand, state())
    assert result.event is not None
    assert result.event.confidence_ratio == pytest.approx(cand.accumulator.confidence_ratio)


def test_a_degraded_module_s_confidence_is_penalised_never_raised(
    metrics: MetricsRegistry,
) -> None:
    config = VerificationConfig(degraded_confidence_penalty_ratio=0.2)
    eng = engine(metrics, chain=(always_pass(),), config=config)
    cand = candidate()
    result = eng.verify(cand, state(config=config, availability=Availability.DEGRADED))
    assert result.event is not None
    assert result.event.confidence_ratio < cand.accumulator.confidence_ratio


# ------------------------------------------------------------------ rejection


def test_the_first_reject_stops_the_chain(metrics: MetricsRegistry) -> None:
    eng = engine(
        metrics,
        chain=(always_pass("first"), always_reject("second"), always_pass("third")),
    )
    result = eng.verify(candidate(), state())
    assert result.outcome is CandidateOutcome.REJECTED
    assert [v.validator_id for v in result.verdicts] == ["first", "second"]
    assert result.event is None


def test_a_rejected_candidate_still_produces_a_record(metrics: MetricsRegistry) -> None:
    """Rejections are data: the only way to tune suppression without flying blind (06 §6)."""
    accumulator = accumulate()
    eng = engine(metrics, chain=(always_reject("zone_gate", "inside an exclusion zone"),))
    result = eng.verify(candidate(), state(), trace=accumulator.trace())
    record = result.record
    assert record is not None
    assert record.outcome is CandidateOutcome.REJECTED
    assert record.rejection_reason is not None
    assert "zone_gate" in record.rejection_reason
    assert "exclusion zone" in record.rejection_reason


def test_a_rejected_record_explains_what_was_considered_and_how_it_evolved(
    metrics: MetricsRegistry,
) -> None:
    """§12: a rejected candidate must answer what, why, how confidence evolved, and why not."""
    accumulator = accumulate(signals=(0.8,) * 13)
    cand = candidate()
    eng = engine(metrics, chain=(always_pass("camera_health"), always_reject("cooldown")))
    result = eng.verify(cand, state(), trace=accumulator.trace())
    record = result.record
    assert record is not None
    assert len(record.candidate.observations) == 13  # what was considered
    assert record.candidate.observations[0].facts["zone_id"] == "yard"  # why
    assert len(record.accumulator_trace) == 13  # how confidence evolved
    assert record.accumulator_trace[-1].log_odds > record.accumulator_trace[0].log_odds
    assert [v.validator_id for v in record.verdicts] == ["camera_health", "cooldown"]
    assert record.rejection_reason is not None  # why not


def test_a_rejection_is_counted_per_validator(metrics: MetricsRegistry) -> None:
    eng = engine(metrics, chain=(always_reject("rate_limit"),))
    eng.verify(candidate(), state())
    assert (
        metrics.counter(
            "vigil_candidate_rejections_total", camera=CAMERA, validator="rate_limit"
        ).value
        == 1.0
    )


def test_a_rejected_candidate_does_not_open_an_event(metrics: MetricsRegistry) -> None:
    config = VerificationConfig()
    ledger = SuppressionLedger(config)
    eng = engine(metrics, chain=(always_reject(),), config=config, ledger=ledger)
    eng.verify(candidate(), state(ledger=ledger, config=config))
    assert len(ledger) == 0


# ------------------------------------------------------------------ fail closed


def test_a_validator_that_raises_rejects_rather_than_passes(metrics: MetricsRegistry) -> None:
    """An objection we could not evaluate is not an absence of objection (01 §6)."""
    eng = engine(metrics, chain=(always_raise("broken"), always_pass("never_runs")))
    result = eng.verify(candidate(), state())
    assert result.outcome is CandidateOutcome.REJECTED
    assert "RuntimeError" in result.verdicts[-1].reason
    assert [v.validator_id for v in result.verdicts] == ["validate_broken"]


def test_a_raising_validator_is_counted_not_swallowed(metrics: MetricsRegistry) -> None:
    eng = engine(metrics, chain=(always_raise(),))
    eng.verify(candidate(), state())
    assert metrics.counter("vigil_stage_errors_total", stage="verify", camera=CAMERA).value == 1.0


# ------------------------------------------------------------------ deferral


def test_a_deferred_candidate_is_held_not_decided(metrics: MetricsRegistry) -> None:
    eng = engine(metrics, chain=(always_pass("a"), always_defer("track_quality")))
    result = eng.verify(candidate(), state())
    assert result.outcome is CandidateOutcome.PENDING
    assert result.is_pending
    assert result.deferred_by == "track_quality"
    assert result.event is None and result.record is None
    assert eng.pending_count == 1


def test_a_deferred_candidate_can_later_be_verified(metrics: MetricsRegistry) -> None:
    gate = {"defer": True}

    def flaky(c: CandidateEvent, s: VerificationState):  # type: ignore[no-untyped-def]
        return deferred("gate", "waiting") if gate["defer"] else passed("gate", "ready now")

    flaky.__name__ = "validate_gate"
    eng = engine(metrics, chain=(flaky,))
    cand = candidate()
    assert eng.verify(cand, state()).is_pending
    gate["defer"] = False
    results = eng.retry_pending(lambda _c: state(now_ms=2000.0))
    assert len(results) == 1
    assert results[0].outcome is CandidateOutcome.VERIFIED
    assert eng.pending_count == 0


def test_a_deferral_that_never_resolves_expires(metrics: MetricsRegistry) -> None:
    config = VerificationConfig(max_defer_ms=5000)
    eng = engine(metrics, chain=(always_defer(),), config=config)
    cand = candidate()
    assert eng.verify(cand, state(config=config, now_ms=0.0)).is_pending
    result = eng.verify(cand, state(config=config, now_ms=6000.0))
    assert result.outcome is CandidateOutcome.EXPIRED
    assert result.record is not None
    assert result.record.rejection_reason is not None
    assert DEFERRAL_EXPIRED in result.record.rejection_reason
    assert eng.pending_count == 0


def test_an_expired_deferral_keeps_the_verdict_history(metrics: MetricsRegistry) -> None:
    config = VerificationConfig(max_defer_ms=1000)
    eng = engine(metrics, chain=(always_pass("health"), always_defer("quality")), config=config)
    cand = candidate()
    eng.verify(cand, state(config=config, now_ms=0.0))
    result = eng.verify(cand, state(config=config, now_ms=5000.0))
    assert result.record is not None
    assert [v.validator_id for v in result.record.verdicts] == ["health", "quality"]
    assert result.record.verdicts[-1].outcome is ValidatorOutcome.REJECT


def test_a_rejection_after_a_deferral_clears_the_hold(metrics: MetricsRegistry) -> None:
    mode = {"defer": True}

    def gate(c: CandidateEvent, s: VerificationState):  # type: ignore[no-untyped-def]
        return deferred("gate", "waiting") if mode["defer"] else rejected("gate", "no")

    gate.__name__ = "validate_gate"
    eng = engine(metrics, chain=(gate,))
    cand = candidate()
    eng.verify(cand, state())
    mode["defer"] = False
    result = eng.verify(cand, state(now_ms=500.0))
    assert result.outcome is CandidateOutcome.REJECTED
    assert eng.pending_count == 0


def test_held_candidates_for_a_camera_can_be_dropped(metrics: MetricsRegistry) -> None:
    """On a tracker epoch change, subject identities are meaningless and so are held candidates."""
    eng = engine(metrics, chain=(always_defer(),))
    eng.verify(candidate(subject=SubjectKey("track", "1")), state())
    eng.verify(candidate(subject=SubjectKey("track", "2")), state())
    assert eng.pending_count == 2
    dropped = eng.drop_pending_for_camera(CAMERA)
    assert len(dropped) == 2
    assert eng.pending_count == 0


def test_pending_ids_are_deterministic(metrics: MetricsRegistry) -> None:
    eng = engine(metrics, chain=(always_defer(),))
    for i in (3, 1, 2):
        eng.verify(candidate(subject=SubjectKey("track", str(i))), state())
    assert list(eng.pending_ids()) == sorted(eng.pending_ids())


# ------------------------------------------------------------------ the real chain end to end


def full_scene() -> SceneStateBuilder:
    return (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard", ZoneKind.RESTRICTED, criticality=3)
        .with_track(
            1,
            at=(0.7, 0.6),
            hits=12,
            age_frames=12,
            in_zones=["yard"],
            first_seen_ms=0.0,
            velocity_px_s=(46.0, 0.0),
        )
        .at_time(1200.0)
    )


def test_a_clean_candidate_passes_the_whole_real_chain(metrics: MetricsRegistry) -> None:
    eng = VerificationEngine(
        ledger=SuppressionLedger(VerificationConfig()),
        config=VerificationConfig(),
        logger=get_logger("test.verify"),
        metrics=metrics,
    )
    result = eng.verify(candidate(), state(scene=full_scene().build(), now_ms=1200.0))
    assert result.outcome is CandidateOutcome.VERIFIED, [
        (v.validator_id, v.outcome.value, v.reason) for v in result.verdicts
    ]
    assert len(result.verdicts) == 11


def test_an_unhealthy_camera_is_caught_by_the_real_chain_first(
    metrics: MetricsRegistry,
) -> None:
    eng = VerificationEngine(
        ledger=SuppressionLedger(VerificationConfig()),
        config=VerificationConfig(),
        logger=get_logger("test.verify"),
        metrics=metrics,
    )
    result = eng.verify(
        candidate(),
        state(
            scene=full_scene().build(),
            camera_health=unhealthy(CameraState.RECONNECTING),
            now_ms=1200.0,
        ),
    )
    assert result.outcome is CandidateOutcome.REJECTED
    assert result.verdicts[0].validator_id == "camera_health"
    assert len(result.verdicts) == 1  # nothing after it ran
