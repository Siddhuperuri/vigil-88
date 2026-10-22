"""Arbitration and severity scoring for one tick (06 §7, §8).

Split out of `analysis.py` because that file reached the repository's 500-line ceiling, and
because this step has one job the rest of the chain does not: it is the only place a
suppression *decided* by arbitration is *acted on*.

Until P6 it was not acted on anywhere. `arbitrate()` computed `suppressed_by`, the analysis
stage put it in a log line, and the response worker went on to create an incident for every
scored event — so 06 §7's subsume rule suppressed nothing, and a `RESTRICTED_AREA_ENTRY` inside
an active `INTRUSION` would have produced the two incidents for one situation that the rule
exists to prevent. Here a suppressed event:

  * is kept out of `scored`, so no incident, no evidence capture and no alert follow it;
  * has its `CandidateRecord` rewritten from VERIFIED to **SUPPRESSED** with arbitration's
    reason, so the candidate table does not claim a verification the incident table cannot
    account for (05 §5 defines that outcome for exactly this);
  * has its subject reported back, so the caller keeps it out of the ongoing-extension pass as
    well — the situation is reported on the dominant event, not twice.

Nothing else in the system constructs a `SeverityAssessment`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vigil.config.schema.modules import ArbitrationConfig
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import ns_to_ms
from vigil.domain.event import CandidateEvent, CandidateRecord, VerifiedEvent
from vigil.domain.incident import SeverityAssessment
from vigil.domain.observation import SubjectKey
from vigil.domain.scene import SceneState
from vigil.severity.engine import SeverityScorer
from vigil.severity.factors import FactorInputs
from vigil.verification.arbitration import ArbitrationOutcome, arbitrate, suppressed_record


@dataclass(frozen=True, slots=True)
class ScoredEvent:
    """A verified event with its severity: the analysis stage's output, the response's input."""

    event: VerifiedEvent
    assessment: SeverityAssessment
    arbitration: ArbitrationOutcome

    @property
    def candidate(self) -> CandidateEvent:
        return self.event.candidate

    @property
    def detection_latency_ms(self) -> float:
        """`confirmed - first_observed`: the headline quality metric for the temporal engine."""
        accumulator = self.event.candidate.accumulator
        return ns_to_ms(
            self.event.candidate.promoted_monotonic_ns - accumulator.first_observation_ns
        )


@dataclass(frozen=True, slots=True)
class ScoringResult:
    """What the step decided. `records` replaces the caller's list wholesale."""

    scored: tuple[ScoredEvent, ...]
    records: tuple[CandidateRecord, ...]
    suppressed_subjects: frozenset[SubjectKey]


def score_tick(
    events: Sequence[VerifiedEvent],
    records: Sequence[CandidateRecord],
    *,
    scene: SceneState,
    now_ns: int,
    config: ArbitrationConfig,
    scorer: SeverityScorer,
    camera_trust_ratio: float,
    logger: LoggerLike,
) -> ScoringResult:
    """Arbitrate, drop what was suppressed, then score what remains."""
    if not events:
        return ScoringResult((), tuple(records), frozenset())
    outcomes = arbitrate(events, config=config, now_monotonic_ns=now_ns)
    suppressions = {o.event.candidate.candidate_id: o for o in outcomes if o.suppressed}
    scored: list[ScoredEvent] = []
    for outcome in outcomes:
        if outcome.suppressed:
            logger.info(
                "event_suppressed_by_arbitration",
                candidate_id=outcome.event.candidate.candidate_id,
                event_type=outcome.event.candidate.event_type.value,
                suppressed_by=outcome.suppressed_by,
                reason="; ".join(outcome.notes),
            )
            continue
        assessment = scorer.score(
            FactorInputs(
                event=outcome.event,
                now_wall_utc=scene.frame_meta.wall_utc,
                zones=scene.zones,
                camera_trust_ratio=camera_trust_ratio,
                related_event_count=max(outcome.escalation_count - 1, 0),
            )
        )
        scored.append(ScoredEvent(event=outcome.event, assessment=assessment, arbitration=outcome))
        logger.info(
            "event_scored",
            candidate_id=outcome.event.candidate.candidate_id,
            severity=assessment.severity.name,
            severity_score=round(assessment.score, 2),
            merged_from=list(outcome.merged_from),
        )
    rewritten = tuple(
        suppressed_record(r, suppressions[r.candidate.candidate_id])
        if r.candidate.candidate_id in suppressions
        else r
        for r in records
    )
    subjects = frozenset(o.event.candidate.subject for o in suppressions.values())
    return ScoringResult(tuple(scored), rewritten, subjects)
