"""Wiring the event chain for one camera (04 §1, 01 §5).

This is the only place the five L4 engines meet. They do not import each other — `events/`
knows nothing of `temporal/`, `verification/` knows nothing of `severity/` — so the stage order
lives here, in L5, where orchestration belongs:

    scene -> observe -> accumulate -> promote -> verify -> arbitrate -> score

and the one thing that must never exist is a shortcut past any of those arrows. There is no path
from a module to an incident (D-003): this file is where that is enforced in code rather than
asserted in a document.

P3 stops at a scored, verified event. P4 owns incidents, evidence and persistence; nothing here
creates an `Incident`, writes a file, or touches a database.

Runs on the camera's analysis thread, which is the only thread allowed to touch this state
(D-005), so nothing here locks. No decision reads a clock: every timestamp comes from the
scene's frame.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from vigil.config.schema.modules import ArbitrationConfig, TemporalConfig, VerificationConfig
from vigil.core.capabilities import Availability
from vigil.core.errors import ModuleError
from vigil.core.ids import UlidFactory
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import ns_to_ms
from vigil.domain.camera import CameraHealth
from vigil.domain.enums import CandidateOutcome, EventType
from vigil.domain.event import (
    AccumulatorSample,
    CandidateEvent,
    CandidateRecord,
    VerifiedEvent,
)
from vigil.domain.incident import SeverityAssessment
from vigil.domain.observation import Observation, SubjectKey
from vigil.domain.scene import SceneState
from vigil.events.engine import EventEngine, ModuleFailure
from vigil.events.promotion import promote
from vigil.observability.metrics import MetricsRegistry
from vigil.severity.engine import SeverityScorer
from vigil.severity.factors import FactorInputs
from vigil.temporal.store import AccumulatorStore
from vigil.verification.arbitration import ArbitrationOutcome, arbitrate
from vigil.verification.engine import VerificationEngine
from vigil.verification.ledger import SuppressionLedger, dedup_key
from vigil.verification.validators.context import Mute, VerificationState


@dataclass(frozen=True, slots=True)
class ScoredEvent:
    """A verified event with its severity. The most P3 produces; P4's input."""

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
class OngoingSituation:
    """An already-confirmed situation that is still producing evidence.

    Promotion happens once per episode -- an active accumulator is not promoted again, which is
    what makes one intrusion one incident. But 06 §9 requires a live incident's `last_observed`
    to follow the evidence, so the fact that evidence is *still arriving* has to reach the
    response stage by some route other than a new candidate. This is that route.

    Throttled by `storage.incident_extend_interval_ms`, because the only thing it changes is a
    timestamp and a duration.
    """

    module_id: str
    event_type: EventType
    subject: SubjectKey
    last_observation_ns: int
    confidence_ratio: float
    log_odds: float


@dataclass(frozen=True, slots=True)
class AnalysisTick:
    """Everything one analysed frame produced. Deterministic given the same scene and state.

    `scene` travels with the tick because P4's evidence needs the scene *at confirmation* -- the
    tracks and zones as they were when the decision was made, not as they are when the response
    worker gets round to writing the dump. It is a frozen value holding no pixels, so carrying it
    across the queue costs a reference.
    """

    camera_id: str
    t_monotonic_ns: int
    observations: tuple[Observation, ...] = ()
    scored: tuple[ScoredEvent, ...] = ()
    records: tuple[CandidateRecord, ...] = ()
    released: tuple[SubjectKey, ...] = ()
    module_failures: tuple[ModuleFailure, ...] = ()
    epoch_reset: bool = False
    active_candidates: int = 0
    ongoing: tuple[OngoingSituation, ...] = ()
    scene: SceneState | None = None
    tracker_epoch: int = 0
    wall_utc: datetime | None = None

    @property
    def verified(self) -> tuple[VerifiedEvent, ...]:
        return tuple(s.event for s in self.scored)

    @property
    def rejected(self) -> tuple[CandidateRecord, ...]:
        return tuple(r for r in self.records if r.outcome is not CandidateOutcome.VERIFIED)

    @property
    def has_work(self) -> bool:
        """Whether this tick is worth handing to the response worker at all."""
        return bool(self.scored or self.records or self.released or self.ongoing)


class AnalysisPipeline:
    """One per camera. Owns that camera's accumulators and module runtimes."""

    __slots__ = (
        "_arbitration",
        "_camera_health",
        "_camera_id",
        "_camera_trust",
        "_clock",
        "_events",
        "_extend_interval_ms",
        "_extended_ns",
        "_instability_since_ns",
        "_ledger",
        "_log",
        "_metrics",
        "_mutes",
        "_scorer",
        "_store",
        "_temporal",
        "_ulids",
        "_verification",
        "_verification_config",
    )

    def __init__(
        self,
        *,
        camera_id: str,
        events: EventEngine,
        store: AccumulatorStore,
        verification: VerificationEngine,
        ledger: SuppressionLedger,
        scorer: SeverityScorer,
        temporal_config: TemporalConfig,
        verification_config: VerificationConfig,
        arbitration_config: ArbitrationConfig,
        ulids: UlidFactory,
        logger: LoggerLike,
        metrics: MetricsRegistry,
        camera_trust_ratio: float = 1.0,
        extend_interval_ms: float = 1000.0,
    ) -> None:
        self._camera_id = camera_id
        self._events = events
        self._store = store
        self._verification = verification
        self._ledger = ledger
        self._scorer = scorer
        self._temporal = temporal_config
        self._verification_config = verification_config
        self._arbitration = arbitration_config
        self._ulids = ulids
        self._log = logger.bind(camera_id=camera_id)
        self._metrics = metrics
        self._camera_trust = camera_trust_ratio
        self._extend_interval_ms = extend_interval_ms
        self._mutes: tuple[Mute, ...] = ()
        self._camera_health: CameraHealth | None = None
        self._instability_since_ns: int | None = None
        self._extended_ns: dict[tuple[str, SubjectKey], int] = {}

    @property
    def camera_id(self) -> str:
        return self._camera_id

    @property
    def active_candidates(self) -> int:
        return len(self._store)

    def set_mutes(self, mutes: Sequence[Mute]) -> None:
        """Operator mutes are pipeline state, not frame state (06 §10)."""
        self._mutes = tuple(mutes)

    def set_camera_health(self, health: CameraHealth | None) -> None:
        """Health comes from the ingest engine, which this one must not import (01 §5)."""
        self._camera_health = health

    # ---------------------------------------------------------------- the tick

    def process(self, scene: SceneState) -> AnalysisTick:
        """Run the whole chain for one analysed frame."""
        now_ns = scene.frame_meta.t_monotonic_ns
        self._note_stability(scene)

        observed = self._events.observe(scene)
        if observed.epoch_reset:
            self._on_epoch_reset()

        # Time passes for every accumulator, not only the observed ones: absence of evidence is
        # itself evidence, and an unobserved accumulator that froze would stay influential.
        self._store.decay_all_to(now_ns)
        for observation in observed.observations:
            self._store.observe(observation)

        records: list[CandidateRecord] = []
        verified: list[VerifiedEvent] = []
        for candidate in self._promote_ready(now_ns, scene):
            result = self._verification.verify(
                candidate,
                self._state_for(candidate, scene, now_ns),
                trace=self._trace_for(candidate),
                wall_utc=scene.frame_meta.wall_utc,
            )
            if result.record is not None:
                records.append(result.record)
            if result.event is not None:
                verified.append(result.event)

        for result in self._verification.retry_pending(lambda c: self._state_for(c, scene, now_ns)):
            if result.record is not None:
                records.append(result.record)
            if result.event is not None:
                verified.append(result.event)

        scored = self._score(verified, scene, now_ns)
        ongoing = self._ongoing(now_ns, exclude={s.candidate.subject for s in scored})
        released = self._release(now_ns)

        self._store.prune_idle(now_ns)
        self._ledger.prune(now_ns)
        self._publish_metrics()

        return AnalysisTick(
            camera_id=self._camera_id,
            t_monotonic_ns=now_ns,
            observations=observed.observations,
            scored=scored,
            records=tuple(records),
            released=released,
            ongoing=ongoing,
            module_failures=observed.failures,
            epoch_reset=observed.epoch_reset,
            active_candidates=len(self._store),
            scene=scene,
            tracker_epoch=max((t.tracker_epoch for t in scene.tracks), default=0),
            wall_utc=scene.frame_meta.wall_utc,
        )

    # ---------------------------------------------------------------- stages

    def _promote_ready(self, now_ns: int, scene: SceneState) -> tuple[CandidateEvent, ...]:
        """Build a candidate for every accumulator that crossed activation.

        The accumulator is marked active here, before verification runs. That is deliberate: a
        rejected candidate must not be re-promoted on the very next frame, which would turn one
        suppressed situation into an endless stream of identical rejections.
        """
        candidates: list[CandidateEvent] = []
        for accumulator in self._store.promotion_ready(now_ns):
            module_id = accumulator.key[1]
            event_type = self._event_type_for(module_id)
            if event_type is None:
                continue
            state = accumulator.state_at(now_ns)
            candidate = promote(
                state=state,
                event_type=event_type,
                observations=accumulator.window(),
                promoted_monotonic_ns=now_ns,
                promoted_wall_utc=scene.frame_meta.wall_utc,
                ulids=self._ulids,
            )
            accumulator.mark_active(now_ns)
            candidates.append(candidate)
            self._metrics.counter(
                "vigil_candidates_promoted_total", camera=self._camera_id, module=module_id
            ).inc()
            self._log.info(
                "candidate_promoted",
                candidate_id=candidate.candidate_id,
                module_id=module_id,
                subject=str(state.subject),
                log_odds=round(state.log_odds, 4),
                confidence_ratio=round(state.confidence_ratio, 4),
                observations=state.observation_count,
                support_ratio=round(state.support_ratio, 4),
            )
        return tuple(candidates)

    def _score(
        self, events: Sequence[VerifiedEvent], scene: SceneState, now_ns: int
    ) -> tuple[ScoredEvent, ...]:
        """Arbitrate, then score. Nothing else in the system constructs a SeverityAssessment."""
        if not events:
            return ()
        outcomes = arbitrate(events, config=self._arbitration, now_monotonic_ns=now_ns)
        scored: list[ScoredEvent] = []
        for outcome in outcomes:
            assessment = self._scorer.score(
                FactorInputs(
                    event=outcome.event,
                    now_wall_utc=scene.frame_meta.wall_utc,
                    zones=scene.zones,
                    camera_trust_ratio=self._camera_trust,
                    related_event_count=max(outcome.escalation_count - 1, 0),
                )
            )
            scored.append(
                ScoredEvent(event=outcome.event, assessment=assessment, arbitration=outcome)
            )
            self._log.info(
                "event_scored",
                candidate_id=outcome.event.candidate.candidate_id,
                severity=assessment.severity.name,
                severity_score=round(assessment.score, 2),
                suppressed_by=outcome.suppressed_by,
            )
        return tuple(scored)

    def _ongoing(self, now_ns: int, *, exclude: set[SubjectKey]) -> tuple[OngoingSituation, ...]:
        """Active accumulators still receiving evidence, at most one report per interval.

        A subject that produced a scored event on this very tick is excluded: that event
        already carries its own timestamps, and reporting both would make the response stage
        extend an incident it is about to create.
        """
        interval_ms = self._extend_interval_ms
        out: list[OngoingSituation] = []
        for accumulator in self._store:
            if not accumulator.is_active:
                continue
            _camera_id, module_id, subject = accumulator.key
            if subject in exclude:
                continue
            last_ns = accumulator.last_observation_ns
            if last_ns is None:
                continue
            key = (module_id, subject)
            previous = self._extended_ns.get(key)
            if previous is not None and ns_to_ms(last_ns - previous) < interval_ms:
                continue
            if previous == last_ns:
                continue  # no new observation since the last report
            event_type = self._event_type_for(module_id)
            if event_type is None:
                continue
            self._extended_ns[key] = last_ns
            state = accumulator.state_at(now_ns)
            out.append(
                OngoingSituation(
                    module_id=module_id,
                    event_type=event_type,
                    subject=subject,
                    last_observation_ns=last_ns,
                    confidence_ratio=state.confidence_ratio,
                    log_odds=state.log_odds,
                )
            )
        return tuple(out)

    def _release(self, now_ns: int) -> tuple[SubjectKey, ...]:
        """End situations whose evidence has gone, starting their cooldown.

        This is what stops one persistent situation producing an endless stream of events, and
        what makes a genuine recurrence a new event rather than a reopened one (06 §9).
        """
        released: list[SubjectKey] = []
        for accumulator in self._store.release_ready(now_ns):
            camera_id, module_id, subject = accumulator.key
            event_type = self._event_type_for(module_id)
            accumulator.mark_released()
            if event_type is not None:
                closed = self._ledger.close(dedup_key(camera_id, event_type, subject), now_ns)
                if closed is not None:
                    self._log.info(
                        "event_released",
                        candidate_id=closed.candidate_id,
                        subject=str(subject),
                        duration_ms=round(closed.duration_ms(now_ns), 1),
                        extensions=closed.extensions,
                    )
            released.append(subject)
            self._extended_ns.pop((module_id, subject), None)
        return tuple(released)

    def _on_epoch_reset(self) -> None:
        """Identities no longer mean what they meant, so everything keyed by them is worthless."""
        self._store.reset()
        self._extended_ns.clear()
        self._ledger.reset_camera(self._camera_id)
        dropped = self._verification.drop_pending_for_camera(self._camera_id)
        self._metrics.counter("vigil_epoch_resets_total", camera=self._camera_id).inc()
        self._log.info("analysis_state_reset_on_epoch_change", dropped_pending=len(dropped))

    # ---------------------------------------------------------------- helpers

    def _event_type_for(self, module_id: str) -> EventType | None:
        try:
            return self._events.runtime(module_id).descriptor.event_type
        except ModuleError:
            # Evidence survives its module being unbound (a quarantine, a reconfiguration). It
            # cannot be promoted without an event type, so it decays instead of being guessed at.
            self._log.warning("no_event_type_for_module", module_id=module_id)
            return None

    def _availability_for(self, module_id: str) -> tuple[Availability, tuple[str, ...]]:
        try:
            runtime = self._events.runtime(module_id)
        except ModuleError:
            return (Availability.ACTIVE, ())
        entry = runtime.availability
        return (entry.status, tuple(sorted(c.value for c in entry.missing_optional)))

    def _trace_for(self, candidate: CandidateEvent) -> tuple[AccumulatorSample, ...]:
        accumulator = self._store.get(candidate.module_id, candidate.subject)
        return () if accumulator is None else accumulator.trace()

    def _note_stability(self, scene: SceneState) -> None:
        """Remember when the view last became unstable, so the blackout window can be measured.

        Lights switching on and a bumped camera are the largest single source of correlated
        false positives (06 §6), and the blackout outlasts the instability itself: the tracker
        needs time to stop producing nonsense after the view settles.
        """
        from vigil.domain.enums import SceneFact

        if SceneFact.STABILITY in scene.degraded_facts:
            return
        if scene.stability_ratio <= self._verification_config.min_stability_ratio:
            self._instability_since_ns = scene.frame_meta.t_monotonic_ns

    def _state_for(
        self, candidate: CandidateEvent, scene: SceneState, now_ns: int
    ) -> VerificationState:
        availability, missing = self._availability_for(candidate.module_id)
        instability = (
            {}
            if self._instability_since_ns is None
            else {self._camera_id: self._instability_since_ns}
        )
        return VerificationState(
            config=self._verification_config,
            ledger=self._ledger,
            now_monotonic_ns=now_ns,
            camera_health=self._camera_health,
            scene=scene,
            module_availability=availability,
            missing_optional=missing,
            mutes=self._mutes,
            instability_since_monotonic_ns=instability,
        )

    def _publish_metrics(self) -> None:
        self._metrics.gauge("vigil_active_candidates", camera=self._camera_id).set(
            float(len(self._store))
        )
        self._metrics.gauge("vigil_open_events", camera=self._camera_id).set(
            float(len(self._ledger))
        )
        self._metrics.gauge("vigil_pending_verifications", camera=self._camera_id).set(
            float(self._verification.pending_count)
        )

    def release(self) -> None:
        self._events.release()
