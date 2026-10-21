"""The arbitration-and-scoring step (`pipeline/scoring.py`), and that a suppression is *acted on*.

This is the test that was missing before P6. `arbitrate()` has computed `suppressed_by` since P3
and nothing read it: the response worker created an incident for every scored event, so 06 §7's
subsume rule suppressed nothing and a `RESTRICTED_AREA_ENTRY` inside an active `INTRUSION` would
have produced the two incidents for one situation that the rule exists to prevent.

So these assert the consequence rather than the decision: a suppressed event leaves `scored`,
which is the only input the response stage has, and its record changes outcome so the candidate
table cannot claim a verification the incident table cannot account for.
"""

from __future__ import annotations

import pytest

from tests.support.candidates import CAMERA, candidate, verified
from tests.support.scenes import SceneStateBuilder
from vigil.config.schema.modules import ArbitrationConfig
from vigil.config.schema.severity import SeverityConfig
from vigil.domain.enums import CandidateOutcome, EventType, ObjectClass
from vigil.domain.event import CandidateEvent, CandidateRecord, VerifiedEvent
from vigil.domain.observation import SubjectKey
from vigil.domain.scene import SceneState
from vigil.observability.logging import get_logger
from vigil.pipeline.scoring import score_tick
from vigil.severity.engine import SeverityScorer

TRACK_1 = SubjectKey("track", "1")
CONFIG = ArbitrationConfig()


@pytest.fixture
def scene() -> SceneState:
    return (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard")
        .with_track(1, ObjectClass.PERSON, hits=10, age_frames=10, in_zones=["yard"])
        .build()
    )


def scorer() -> SeverityScorer:
    return SeverityScorer(config=SeverityConfig(), logger=get_logger("test.scoring"))


def record_for(cand: CandidateEvent) -> CandidateRecord:
    """The VERIFIED record the verification engine would have produced for this candidate."""
    return CandidateRecord(
        candidate=cand,
        outcome=CandidateOutcome.VERIFIED,
        verdicts=(),
        accumulator_trace=(),
        rejection_reason=None,
        decided_monotonic_ns=cand.promoted_monotonic_ns,
        decided_wall_utc=cand.promoted_wall_utc,
    )


def run(events: list[VerifiedEvent], scene: SceneState, *, now_ms: float = 1200.0):
    records = [record_for(e.candidate) for e in events]
    return score_tick(
        events,
        records,
        scene=scene,
        now_ns=int(now_ms * 1e6),
        config=CONFIG,
        scorer=scorer(),
        camera_trust_ratio=1.0,
        logger=get_logger("test.scoring"),
    )


def test_nothing_to_score_leaves_the_records_alone(scene: SceneState) -> None:
    records = (record_for(candidate()),)
    result = score_tick(
        [],
        records,
        scene=scene,
        now_ns=0,
        config=CONFIG,
        scorer=scorer(),
        camera_trust_ratio=1.0,
        logger=get_logger("test.scoring"),
    )
    assert result.scored == ()
    assert result.records == records
    assert result.suppressed_subjects == frozenset()


def test_an_unsuppressed_event_is_scored_and_its_record_is_untouched(scene: SceneState) -> None:
    event = verified()
    result = run([event], scene)
    assert len(result.scored) == 1
    assert result.scored[0].candidate is event.candidate
    assert 0.0 <= result.scored[0].assessment.score <= 100.0
    assert result.records[0].outcome is CandidateOutcome.VERIFIED
    assert result.suppressed_subjects == frozenset()


def test_a_subsumed_event_is_not_scored_so_no_incident_can_follow(scene: SceneState) -> None:
    """`scored` is the response stage's only input: leaving it is what prevents the incident."""
    broad = verified(cand=candidate(event_type=EventType.INTRUSION, subject=TRACK_1))
    narrow = verified(cand=candidate(event_type=EventType.RESTRICTED_AREA_ENTRY, subject=TRACK_1))
    result = run([broad, narrow], scene)
    assert [s.candidate.event_type for s in result.scored] == [EventType.INTRUSION]
    assert narrow.candidate.candidate_id not in {s.candidate.candidate_id for s in result.scored}


def test_a_suppressed_events_record_becomes_suppressed_with_the_reason(
    scene: SceneState,
) -> None:
    broad = verified(cand=candidate(event_type=EventType.INTRUSION, subject=TRACK_1))
    narrow = verified(cand=candidate(event_type=EventType.RESTRICTED_AREA_ENTRY, subject=TRACK_1))
    result = run([broad, narrow], scene)
    by_id = {r.candidate.candidate_id: r for r in result.records}
    suppressed = by_id[narrow.candidate.candidate_id]
    assert suppressed.outcome is CandidateOutcome.SUPPRESSED
    assert broad.candidate.candidate_id in (suppressed.rejection_reason or "")
    # The surviving event's record is left exactly as verification wrote it.
    assert by_id[broad.candidate.candidate_id].outcome is CandidateOutcome.VERIFIED


def test_a_suppressed_subject_is_reported_so_it_is_not_extended_as_well(
    scene: SceneState,
) -> None:
    """Otherwise the response stage would try to grow an incident that was never created."""
    broad = verified(cand=candidate(event_type=EventType.INTRUSION, subject=TRACK_1))
    narrow = verified(cand=candidate(event_type=EventType.RESTRICTED_AREA_ENTRY, subject=TRACK_1))
    result = run([broad, narrow], scene)
    assert result.suppressed_subjects == frozenset({TRACK_1})


def test_a_merged_smoke_event_is_not_scored_and_the_fire_names_it(scene: SceneState) -> None:
    """06 §7 merge, end to end through this step: one incident, with smoke recorded on it."""
    fire = verified(cand=candidate(event_type=EventType.FIRE, subject=TRACK_1, module_id="fire"))
    smoke = verified(cand=candidate(event_type=EventType.SMOKE, subject=TRACK_1, module_id="smoke"))
    result = run([fire, smoke], scene)
    assert [s.candidate.event_type for s in result.scored] == [EventType.FIRE]
    assert result.scored[0].arbitration.merged_from == (smoke.candidate.candidate_id,)
    by_id = {r.candidate.candidate_id: r for r in result.records}
    assert by_id[smoke.candidate.candidate_id].outcome is CandidateOutcome.SUPPRESSED


def test_the_escalation_count_still_reaches_the_severity_engine(scene: SceneState) -> None:
    """Escalation is not a suppression: three related events are all scored, and each one's
    `related_event_count` is what the severity factor reads."""
    events = [verified(cand=candidate(subject=SubjectKey("track", str(i)))) for i in range(1, 4)]
    result = run(events, scene)
    assert len(result.scored) == 3
    assert all(s.arbitration.escalation_count == 3 for s in result.scored)
