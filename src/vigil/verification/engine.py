"""Running the validator chain over a candidate (04 §1 `verify`, 06 §6).

The contract:

  * the first REJECT stops the chain and the candidate is refused, with that validator's reason;
  * any DEFER holds the candidate, re-evaluated on each later tick until
    `verification.max_defer_ms` elapses, after which it is rejected as `deferral_expired`;
  * every verdict that ran is kept, including passes;
  * a rejected candidate still produces a `CandidateRecord`, because rejections are the dataset
    for tuning suppression (05 §5).

Fails closed. A validator that raises is treated as a rejection, not as a pass: an objection we
could not evaluate is not an absence of objection.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from vigil.config.schema.modules import VerificationConfig
from vigil.core.capabilities import Availability
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import ns_to_ms
from vigil.domain.enums import CandidateOutcome, ValidatorOutcome
from vigil.domain.event import (
    AccumulatorSample,
    CandidateEvent,
    CandidateRecord,
    ValidatorVerdict,
    VerifiedEvent,
)
from vigil.observability.metrics import MetricsRegistry
from vigil.verification.ledger import SuppressionLedger, dedup_key
from vigil.verification.validators.chain import DEFAULT_CHAIN
from vigil.verification.validators.context import VerificationState, rejected

Validator = Callable[[CandidateEvent, VerificationState], ValidatorVerdict]
DEFERRAL_EXPIRED = "deferral_expired"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """One pass of the chain. Exactly one of `event` and `record.rejection_reason` is set unless
    the outcome is PENDING, in which case the candidate is held."""

    outcome: CandidateOutcome
    verdicts: tuple[ValidatorVerdict, ...]
    event: VerifiedEvent | None = None
    record: CandidateRecord | None = None
    deferred_by: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.outcome is CandidateOutcome.PENDING


@dataclass(slots=True)
class _Pending:
    candidate: CandidateEvent
    trace: tuple[AccumulatorSample, ...]
    first_deferred_ns: int
    last_reason: str


class VerificationEngine:
    """Owns the suppression ledger's lifecycle and the deferral queue."""

    __slots__ = ("_chain", "_config", "_ledger", "_log", "_metrics", "_pending")

    def __init__(
        self,
        *,
        ledger: SuppressionLedger,
        config: VerificationConfig,
        logger: LoggerLike,
        metrics: MetricsRegistry,
        chain: Sequence[Validator] = DEFAULT_CHAIN,
    ) -> None:
        self._ledger = ledger
        self._config = config
        self._log = logger.bind(stage="verify")
        self._metrics = metrics
        self._chain = tuple(chain)
        self._pending: dict[str, _Pending] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._pending))

    # ---------------------------------------------------------------- verify

    def verify(
        self,
        candidate: CandidateEvent,
        state: VerificationState,
        *,
        trace: Sequence[AccumulatorSample] = (),
        wall_utc: datetime | None = None,
    ) -> VerificationResult:
        """Run the chain once. Records the outcome, opens the event on success."""
        verdicts: list[ValidatorVerdict] = []
        decided_wall = wall_utc or candidate.promoted_wall_utc

        for validator in self._chain:
            verdict_value = self._run_one(validator, candidate, state)
            verdicts.append(verdict_value)
            if verdict_value.outcome is ValidatorOutcome.REJECT:
                return self._reject(candidate, tuple(verdicts), verdict_value, trace, decided_wall)
            if verdict_value.outcome is ValidatorOutcome.DEFER:
                return self._defer(
                    candidate, tuple(verdicts), verdict_value, trace, state, decided_wall
                )

        return self._accept(candidate, tuple(verdicts), trace, state, decided_wall)

    def _run_one(
        self, validator: Validator, candidate: CandidateEvent, state: VerificationState
    ) -> ValidatorVerdict:
        try:
            return validator(candidate, state)
        except Exception as exc:  # noqa: BLE001 - fail closed; see the module docstring
            name = getattr(validator, "__name__", "validator")
            self._metrics.counter(
                "vigil_stage_errors_total", stage="verify", camera=candidate.camera_id
            ).inc()
            self._log.error(
                "validator_raised",
                validator=name,
                camera_id=candidate.camera_id,
                candidate_id=candidate.candidate_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return rejected(
                name,
                f"validator raised {type(exc).__name__}: an objection that could not be "
                "evaluated is not an absence of objection",
            )

    # ---------------------------------------------------------------- outcomes

    def _accept(
        self,
        candidate: CandidateEvent,
        verdicts: tuple[ValidatorVerdict, ...],
        trace: Sequence[AccumulatorSample],
        state: VerificationState,
        decided_wall: datetime,
    ) -> VerificationResult:
        self._pending.pop(candidate.candidate_id, None)
        confidence = self._post_verification_confidence(candidate, state)
        event = VerifiedEvent(
            candidate=candidate,
            verdicts=verdicts,
            confidence_ratio=confidence,
            verified_monotonic_ns=state.now_monotonic_ns,
        )
        self._ledger.record_open(
            dedup_key(candidate.camera_id, candidate.event_type, candidate.subject),
            candidate_id=candidate.candidate_id,
            t_monotonic_ns=state.now_monotonic_ns,
        )
        record = CandidateRecord(
            candidate=candidate,
            outcome=CandidateOutcome.VERIFIED,
            verdicts=verdicts,
            accumulator_trace=tuple(trace),
            rejection_reason=None,
            decided_monotonic_ns=state.now_monotonic_ns,
            decided_wall_utc=decided_wall,
        )
        self._metrics.counter(
            "vigil_candidates_total", camera=candidate.camera_id, outcome="verified"
        ).inc()
        self._log.info(
            "candidate_verified",
            camera_id=candidate.camera_id,
            candidate_id=candidate.candidate_id,
            module_id=candidate.module_id,
            subject=str(candidate.subject),
            confidence_ratio=round(confidence, 4),
            validators=len(verdicts),
        )
        return VerificationResult(
            outcome=CandidateOutcome.VERIFIED, verdicts=verdicts, event=event, record=record
        )

    def _post_verification_confidence(
        self, candidate: CandidateEvent, state: VerificationState
    ) -> float:
        """Verification may *penalise* confidence; it can never raise it (06 §6).

        Only accumulation raises confidence. A validator that could add it would be a second,
        hidden promotion path.
        """
        confidence = candidate.accumulator.confidence_ratio
        if state.module_availability is Availability.DEGRADED:
            confidence *= 1.0 - state.config.degraded_confidence_penalty_ratio
        return min(max(confidence, 0.0), 1.0)

    def _reject(
        self,
        candidate: CandidateEvent,
        verdicts: tuple[ValidatorVerdict, ...],
        verdict_value: ValidatorVerdict,
        trace: Sequence[AccumulatorSample],
        decided_wall: datetime,
    ) -> VerificationResult:
        self._pending.pop(candidate.candidate_id, None)
        reason = f"{verdict_value.validator_id}: {verdict_value.reason}"
        record = CandidateRecord(
            candidate=candidate,
            outcome=CandidateOutcome.REJECTED,
            verdicts=verdicts,
            accumulator_trace=tuple(trace),
            rejection_reason=reason,
            decided_monotonic_ns=candidate.promoted_monotonic_ns,
            decided_wall_utc=decided_wall,
        )
        self._metrics.counter(
            "vigil_candidates_total", camera=candidate.camera_id, outcome="rejected"
        ).inc()
        self._metrics.counter(
            "vigil_candidate_rejections_total",
            camera=candidate.camera_id,
            validator=verdict_value.validator_id,
        ).inc()
        self._log.info(
            "candidate_rejected",
            camera_id=candidate.camera_id,
            candidate_id=candidate.candidate_id,
            module_id=candidate.module_id,
            subject=str(candidate.subject),
            validator=verdict_value.validator_id,
            reason=verdict_value.reason,
        )
        return VerificationResult(
            outcome=CandidateOutcome.REJECTED, verdicts=verdicts, record=record
        )

    def _defer(
        self,
        candidate: CandidateEvent,
        verdicts: tuple[ValidatorVerdict, ...],
        verdict_value: ValidatorVerdict,
        trace: Sequence[AccumulatorSample],
        state: VerificationState,
        decided_wall: datetime,
    ) -> VerificationResult:
        existing = self._pending.get(candidate.candidate_id)
        first_ns = existing.first_deferred_ns if existing else state.now_monotonic_ns
        held_ms = ns_to_ms(state.now_monotonic_ns - first_ns)
        if held_ms >= state.config.max_defer_ms:
            expired = rejected(
                verdict_value.validator_id,
                f"{DEFERRAL_EXPIRED}: held for {held_ms:.0f} ms without becoming decidable "
                f"({verdict_value.reason})",
            )
            return self._expire(candidate, (*verdicts[:-1], expired), expired, trace, decided_wall)
        self._pending[candidate.candidate_id] = _Pending(
            candidate=candidate,
            trace=tuple(trace),
            first_deferred_ns=first_ns,
            last_reason=verdict_value.reason,
        )
        self._metrics.counter(
            "vigil_candidates_total", camera=candidate.camera_id, outcome="deferred"
        ).inc()
        return VerificationResult(
            outcome=CandidateOutcome.PENDING,
            verdicts=verdicts,
            deferred_by=verdict_value.validator_id,
        )

    def _expire(
        self,
        candidate: CandidateEvent,
        verdicts: tuple[ValidatorVerdict, ...],
        verdict_value: ValidatorVerdict,
        trace: Sequence[AccumulatorSample],
        decided_wall: datetime,
    ) -> VerificationResult:
        self._pending.pop(candidate.candidate_id, None)
        record = CandidateRecord(
            candidate=candidate,
            outcome=CandidateOutcome.EXPIRED,
            verdicts=verdicts,
            accumulator_trace=tuple(trace),
            rejection_reason=f"{verdict_value.validator_id}: {verdict_value.reason}",
            decided_monotonic_ns=candidate.promoted_monotonic_ns,
            decided_wall_utc=decided_wall,
        )
        self._metrics.counter(
            "vigil_candidates_total", camera=candidate.camera_id, outcome="expired"
        ).inc()
        self._log.info(
            "candidate_expired",
            camera_id=candidate.camera_id,
            candidate_id=candidate.candidate_id,
            validator=verdict_value.validator_id,
            reason=verdict_value.reason,
        )
        return VerificationResult(
            outcome=CandidateOutcome.EXPIRED, verdicts=verdicts, record=record
        )

    # ---------------------------------------------------------------- deferral queue

    def pending_candidates(self) -> tuple[CandidateEvent, ...]:
        return tuple(self._pending[cid].candidate for cid in self.pending_ids())

    def retry_pending(
        self, state_for: Callable[[CandidateEvent], VerificationState]
    ) -> tuple[VerificationResult, ...]:
        """Re-evaluate every held candidate. Called on the analysis tick, which is what lets a
        validator wait for evidence without modelling time itself."""
        results: list[VerificationResult] = []
        for candidate_id in self.pending_ids():
            held = self._pending.get(candidate_id)
            if held is None:
                continue
            results.append(self.verify(held.candidate, state_for(held.candidate), trace=held.trace))
        return tuple(results)

    def drop_pending_for_camera(self, camera_id: str) -> tuple[str, ...]:
        """Forget held candidates for a camera whose identities have been invalidated."""
        dropped = tuple(
            cid for cid, held in self._pending.items() if held.candidate.camera_id == camera_id
        )
        for candidate_id in dropped:
            del self._pending[candidate_id]
        return dropped
