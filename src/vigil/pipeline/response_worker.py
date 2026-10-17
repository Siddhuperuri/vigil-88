"""The response stage: decided events become durable incidents (01 §4, §6, §8).

Everything expensive happens here and nowhere else -- database writes, JPEG and MP4 encoding,
disk I/O, outbound alerts -- because this is the one thread that is allowed to be slow. Analysis
hands work over through a bounded queue and carries on; if this thread stalls, detection keeps
running and the queue's declared policy (block briefly, then spill to a journal) decides what
happens to the backlog.

Order within one tick is deliberate:

  1. **Candidates first.** Every promoted candidate is recorded, whatever became of it. A
     rejection is the dataset that tunes suppression (06 §6), and recording it before the
     incident means a crash between the two leaves the rejection, not a dangling incident.
  2. **Then incidents**, created or extended, one transaction each.
  3. **Then evidence**, opened and held until its post-roll closes.
  4. **Then alerts**, only for newly created incidents, and only above `alerts.min_severity`.
  5. **Then releases**, which walk an incident to RESOLVING and RESOLVED.

The engine fails **open** (01 §6): a failure in evidence or alerting is counted, logged and
recorded on the incident as a partial result -- it never prevents the incident being persisted,
because losing the record of a real incident is the worst outcome available.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import partial

from vigil.config.settings import Settings
from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError, VigilError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import MS_PER_S, NS_PER_MS
from vigil.domain.detection import ModelDescriptor
from vigil.domain.enums import CandidateOutcome, EventType
from vigil.domain.event import AccumulatorSample
from vigil.domain.incident import SystemSnapshot
from vigil.domain.observation import SubjectKey
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.repositories.events import CandidateRepository
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.pipeline.analysis import AnalysisTick, ScoredEvent
from vigil.pipeline.evidence_capture import EvidenceCapture, PendingCapture
from vigil.pipeline.queues import SpillQueue
from vigil.response.channels.registry import AlertDispatcher
from vigil.response.evidence import EvidenceStatus
from vigil.response.incident_manager import (
    IncidentContext,
    IncidentManager,
    IncidentOutcome,
)

ModelLookup = Callable[[], ModelDescriptor | None]
SystemLookup = Callable[[], SystemSnapshot]
# A detector may be absent (the null backend); an incident still needs a descriptor, and an
# honest one says so rather than naming a model that did not run.
UNKNOWN_MODEL = ModelDescriptor(
    name="unknown",
    version="",
    backend="none",
    weights_sha256=None,
    input_size_px=None,
    precision="",
    device="",
    license=None,
)


@dataclass(frozen=True, slots=True)
class ResponseIdentity:
    """Per-process facts copied onto every incident so it stays readable forever (05 §4)."""

    module_versions: Mapping[str, str]
    locations: Mapping[str, str | None]
    caveats: Mapping[str, tuple[str, ...]]
    model: ModelLookup
    system: SystemLookup

    def context_for(self, tick: AnalysisTick, scored: ScoredEvent) -> IncidentContext:
        module_id = scored.candidate.module_id
        return IncidentContext(
            tracker_epoch=tick.tracker_epoch,
            module_version=self.module_versions.get(module_id, "unknown"),
            location=self.locations.get(tick.camera_id),
            model=self.model() or UNKNOWN_MODEL,
            system=self.system(),
            caveats=self.caveats.get(tick.camera_id, ()),
        )


@dataclass(slots=True)
class ResponseStats:
    ticks: int = 0
    incidents_created: int = 0
    incidents_extended: int = 0
    candidates_recorded: int = 0
    evidence_completed: int = 0
    alerts_dispatched: int = 0
    releases: int = 0
    failures: int = 0


class ResponseWorker:
    __slots__ = (
        "_alerts",
        "_candidates",
        "_capture",
        "_clock",
        "_evidence",
        "_identity",
        "_incidents",
        "_log",
        "_metrics",
        "_queue",
        "_settings",
        "_wait_s",
        "stats",
    )

    def __init__(
        self,
        *,
        queue: SpillQueue[AnalysisTick],
        incidents: IncidentManager,
        candidates: CandidateRepository,
        evidence: EvidenceRepository,
        capture: EvidenceCapture,
        alerts: AlertDispatcher,
        identity: ResponseIdentity,
        settings: Settings,
        clock: Clock,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._queue = queue
        self._incidents = incidents
        self._candidates = candidates
        self._evidence = evidence
        self._capture = capture
        self._alerts = alerts
        self._identity = identity
        self._settings = settings
        self._clock = clock
        self._log = logger.bind(stage="respond")
        self._metrics = metrics
        # Short, so an open capture's deadline is noticed promptly even on an idle system.
        self._wait_s = min(0.5, settings.pipeline.worker_idle_wait_ms / MS_PER_S * 10)
        self.stats = ResponseStats()

    # ---------------------------------------------------------------- one tick

    def handle(self, tick: AnalysisTick) -> None:
        """Process one decided tick. Never raises: a failure here must not kill the worker."""
        self.stats.ticks += 1
        incident_ids: dict[str, str] = {}
        for scored in tick.scored:
            outcome = self._record_incident(tick, scored)
            if outcome is not None:
                incident_ids[scored.candidate.candidate_id] = outcome
        self._record_candidates(tick, incident_ids)
        self._record_ongoing(tick)
        self._record_releases(tick)

    def _record_incident(self, tick: AnalysisTick, scored: ScoredEvent) -> str | None:
        candidate = scored.candidate
        try:
            outcome = self._incidents.record(
                scored.event,
                scored.assessment,
                self._identity.context_for(tick, scored),
                accumulator_trace=_trace_for(tick, candidate.candidate_id),
            )
        except (PersistenceError, ValueError) as exc:
            self.stats.failures += 1
            self._metrics.counter("vigil_response_errors_total", step="incident").inc()
            self._log.error(
                "incident_not_recorded",
                candidate_id=candidate.candidate_id,
                camera_id=candidate.camera_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

        if outcome.created:
            self.stats.incidents_created += 1
            self._begin_capture(tick, scored, outcome)
            self._dispatch(outcome)
        elif outcome.extended:
            self.stats.incidents_extended += 1
        return outcome.incident_id

    def _begin_capture(
        self, tick: AnalysisTick, scored: ScoredEvent, outcome: IncidentOutcome
    ) -> None:
        incident_id = outcome.incident_id
        if not self._capture.enabled:
            self._incidents.set_evidence_status(incident_id, EvidenceStatus.DISABLED)
            return
        if outcome.explanation is None:  # pragma: no cover - a created incident always has one
            self._incidents.set_evidence_status(incident_id, EvidenceStatus.UNAVAILABLE)
            return
        candidate = scored.candidate
        accumulator = candidate.accumulator
        pending = self._capture.begin(
            incident_id=incident_id,
            display_ref=outcome.display_ref,
            camera_id=candidate.camera_id,
            first_observed_ns=accumulator.first_observation_ns,
            confirmed_ns=candidate.promoted_monotonic_ns,
            confirmed_wall_utc=candidate.promoted_wall_utc,
            scene=tick.scene,
            # The explanation that was persisted, not a fresh one: an evidence bundle has to
            # agree with the database row it belongs to.
            explanation=outcome.explanation,
            involved_track_ids=candidate.involved_track_ids,
        )
        if pending is None:
            self._incidents.set_evidence_status(incident_id, EvidenceStatus.UNAVAILABLE)

    def _dispatch(self, outcome: IncidentOutcome) -> None:
        result = self._alerts.dispatch(outcome.incident_id, outcome.severity)
        if result.dispatched:
            self.stats.alerts_dispatched += 1
        elif result.suppressed_reason:
            self._log.debug(
                "alert_not_dispatched",
                incident_id=outcome.incident_id,
                reason=result.suppressed_reason,
            )

    def _record_candidates(self, tick: AnalysisTick, incident_ids: Mapping[str, str]) -> None:
        if not tick.records:
            return
        try:
            written = self._candidates.record_all(tick.records, incident_ids=dict(incident_ids))
        except PersistenceError as exc:
            self.stats.failures += 1
            self._metrics.counter("vigil_response_errors_total", step="candidates").inc()
            self._log.error("candidates_not_recorded", error=exc.message)
            return
        self.stats.candidates_recorded += written
        for record in tick.records:
            if record.outcome is not CandidateOutcome.VERIFIED:
                self._metrics.counter(
                    "vigil_candidates_rejected_total",
                    camera=record.camera_id,
                    outcome=record.outcome.value,
                ).inc()

    def _record_ongoing(self, tick: AnalysisTick) -> None:
        """Grow the span of every open incident that is still receiving evidence."""
        if not tick.ongoing or tick.wall_utc is None:
            return
        for situation in tick.ongoing:
            wall = tick.wall_utc + timedelta(
                milliseconds=(situation.last_observation_ns - tick.t_monotonic_ns) / NS_PER_MS
            )
            try:
                extended = self._incidents.extend_ongoing(
                    tick.camera_id,
                    situation.event_type,
                    situation.subject,
                    last_observed_wall_utc=wall,
                    confidence_ratio=situation.confidence_ratio,
                )
            except PersistenceError as exc:
                self.stats.failures += 1
                self._metrics.counter("vigil_response_errors_total", step="extend").inc()
                self._log.error(
                    "incident_not_extended", subject=str(situation.subject), error=exc.message
                )
                continue
            if extended is not None:
                self.stats.incidents_extended += 1

    def _record_releases(self, tick: AnalysisTick) -> None:
        if not tick.released:
            return
        sustain_ms = float(self._settings.temporal.release_sustain_ms)
        for subject in tick.released:
            event_type = self._event_type_for(tick, subject)
            if event_type is None:
                continue
            try:
                released = self._incidents.release(
                    tick.camera_id, event_type, subject, sustain_ms=sustain_ms
                )
            except (PersistenceError, VigilError) as exc:
                self.stats.failures += 1
                self._metrics.counter("vigil_response_errors_total", step="release").inc()
                self._log.error("release_not_recorded", subject=str(subject), error=f"{exc}")
                continue
            if released is not None:
                self.stats.releases += 1

    @staticmethod
    def _event_type_for(tick: AnalysisTick, subject: SubjectKey) -> EventType | None:
        """A release names a subject, not an event type; the scored events in the same tick, or
        the module that owns the subject, supply it. When neither does, there is nothing to
        close and guessing would resolve an unrelated incident."""
        for scored in tick.scored:
            if scored.candidate.subject == subject:
                return scored.candidate.event_type
        return None

    # ---------------------------------------------------------------- evidence deadlines

    def complete_due_captures(self, *, now_ns: int | None = None) -> int:
        """Write evidence for every capture whose post-roll window has closed."""
        at = self._clock.monotonic_ns() if now_ns is None else now_ns
        completed = 0
        for pending in self._capture.due(at):
            self._finish_capture(pending)
            completed += 1
        return completed

    def _finish_capture(self, pending: PendingCapture) -> None:
        report = self._capture.complete(pending)
        try:
            if report.records:
                self._evidence.add_all(report.records)
        except PersistenceError as exc:
            self.stats.failures += 1
            self._metrics.counter("vigil_response_errors_total", step="evidence_rows").inc()
            self._log.error(
                "evidence_rows_not_recorded",
                incident_id=pending.incident_id,
                error=exc.message,
            )
            # The files are on disk and the incident is real: say partial rather than complete.
            self._incidents.set_evidence_status(pending.incident_id, EvidenceStatus.PARTIAL)
            return
        self._incidents.set_evidence_status(pending.incident_id, report.status)
        self.stats.evidence_completed += 1
        self._metrics.counter(
            "vigil_evidence_captures_completed_total", status=report.status.value
        ).inc()

    def flush(self) -> int:
        """At shutdown: drain the queue, then write every open capture with what exists.

        Nothing confirmed is abandoned. A clip that is shorter than configured says so in its
        own manifest, which is the honest outcome of being shut down mid-capture.
        """
        drained = 0
        for tick in self._queue.drain():
            self._safely(partial(self.handle, tick), "flush_tick")
            drained += 1
        for pending in self._capture.take_all():
            self._safely(partial(self._finish_capture, pending), "flush_capture")
        return drained

    def _safely(self, action: Callable[[], object], step: str) -> None:
        try:
            action()
        except VigilError as exc:
            self.stats.failures += 1
            self._metrics.counter("vigil_response_errors_total", step=step).inc()
            self._log.error("response_step_failed", step=step, error=exc.message)

    # ---------------------------------------------------------------- the thread

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        while not stop.is_set():
            beat()
            tick = self._queue.take(self._wait_s)
            if tick is not None:
                self._safely(partial(self.handle, tick), "tick")
            self._safely(self.complete_due_captures, "captures")
            self._metrics.gauge("vigil_queue_depth", queue="response").set(
                float(self._queue.depth())
            )
        self.flush()

    def spill(self, tick: AnalysisTick) -> None:
        """The spill sink for the response queue: record what we can, where we can.

        Called on the *analysis* thread when the queue stayed full past its timeout, so it must
        be fast and must not touch the database. The tick is written to a journal line instead,
        which is recoverable by hand and, crucially, counted.
        """
        self._metrics.counter("vigil_response_spilled_total", camera=tick.camera_id).inc()
        self._log.error(
            "response_queue_overflow",
            camera_id=tick.camera_id,
            scored=len(tick.scored),
            records=len(tick.records),
            released=len(tick.released),
            t_monotonic_ns=tick.t_monotonic_ns,
            wall_utc=None if tick.wall_utc is None else tick.wall_utc.isoformat(),
        )


def _trace_for(tick: AnalysisTick, candidate_id: str) -> tuple[AccumulatorSample, ...]:
    """The accumulator trajectory recorded for this candidate, from the tick's own records.

    The trace lives on the `CandidateRecord` rather than on the event, because it is the account
    of how confidence evolved and is written for rejections too (05 §5).
    """
    for record in tick.records:
        if record.candidate.candidate_id == candidate_id:
            return record.accumulator_trace
    return ()
