"""The incident lifecycle: dedup, duplicate delivery, legal transitions, and retention.

The properties here are the ones that make an incident record trustworthy. If dedup fails, one
situation becomes sixty incidents and the desk is useless. If a terminal state is not terminal,
`duration_ms` stops meaning anything and historical analysis is incoherent (06 §9). If retention
orphans an incident from its evidence, the console offers downloads that 404.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vigil.config.loader import load_settings
from vigil.config.schema.storage import StorageConfig
from vigil.core.clock import ManualClock
from vigil.core.errors import PersistenceError
from vigil.core.ids import UlidFactory
from vigil.core.rng import seeded_rng
from vigil.domain.detection import ModelDescriptor
from vigil.domain.enums import (
    CandidateOutcome,
    EventType,
    EvidenceKind,
    IncidentStatus,
    Severity,
    ValidatorOutcome,
)
from vigil.domain.event import (
    AccumulatorSample,
    AccumulatorState,
    CandidateEvent,
    CandidateRecord,
    ValidatorVerdict,
    VerifiedEvent,
)
from vigil.domain.evidence import Evidence
from vigil.domain.incident import SeverityAssessment, SeverityFactor, SystemSnapshot
from vigil.domain.observation import Observation, SubjectKey
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.database import Database
from vigil.persistence.queries import IncidentFilter
from vigil.persistence.repositories.events import CandidateRepository, HealthEventRepository
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.persistence.repositories.incidents import IncidentRepository
from vigil.persistence.repositories.metrics import MetricRepository
from vigil.persistence.repositories.mutes import MuteRepository
from vigil.response.evidence import EvidenceStatus
from vigil.response.incident_manager import (
    IncidentContext,
    IncidentManager,
    TransitionRefused,
    dedup_key_text,
)
from vigil.response.operator_actions import OperatorActions
from vigil.response.retention import RetentionRepositories, RetentionWorker

CAMERA = "gate-north"
SUBJECT = SubjectKey("track", "42")
WALL = datetime(2026, 3, 1, 2, 41, tzinfo=UTC)
MODEL = ModelDescriptor("yolox_s", "1.0", "onnxruntime", None, 640, "fp32", "cuda", "Apache-2.0")
SYSTEM = SystemSnapshot("0.1.0", "c" * 64, "cuda", 14.2, 62.0)
CONTEXT = IncidentContext(
    tracker_epoch=1,
    module_version="1.0.0",
    location="North gate, pole 3",
    model=MODEL,
    system=SYSTEM,
    caveats=("intrusion: running without velocity",),
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Database]:
    database = Database(
        tmp_path / "vigil.sqlite3",
        config=StorageConfig(),
        clock=ManualClock(wall_start=WALL),
        logger=get_logger("test.db"),
        metrics=MetricsRegistry(),
    )
    database.open()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def manager(store: Database) -> IncidentManager:
    return IncidentManager(
        incidents=IncidentRepository(store),
        settings=load_settings(env={}, use_dotenv=False, config_dir=Path("config")).settings,
        clock=ManualClock(wall_start=WALL),
        ulids=UlidFactory(ManualClock(wall_start=WALL), seeded_rng(9, "incidents")),
        logger=get_logger("test.incidents"),
        metrics=MetricsRegistry(),
    )


@pytest.fixture
def actions(store: Database, manager: IncidentManager) -> OperatorActions:
    return OperatorActions(
        manager=manager,
        incidents=IncidentRepository(store),
        clock=ManualClock(wall_start=WALL),
        logger=get_logger("test.operator"),
        metrics=MetricsRegistry(),
    )


def _event(
    *,
    candidate_id: str,
    at: datetime = WALL,
    first_ns: int = 1_000_000_000,
    last_ns: int = 1_900_000_000,
    event_type: EventType = EventType.INTRUSION,
    subject: SubjectKey = SUBJECT,
) -> VerifiedEvent:
    accumulator = AccumulatorState(
        camera_id=CAMERA,
        module_id="intrusion",
        subject=subject,
        log_odds=3.1,
        confidence_ratio=0.92,
        observation_count=9,
        support_ratio=0.9,
        first_observation_ns=first_ns,
        last_observation_ns=last_ns,
        is_active=True,
    )
    observation = Observation(
        camera_id=CAMERA,
        module_id="intrusion",
        subject=subject,
        signal=0.7,
        t_monotonic_ns=last_ns,
        facts={"zone_id": "yard", "object_class": "person"},
    )
    candidate = CandidateEvent(
        candidate_id=candidate_id,
        camera_id=CAMERA,
        module_id="intrusion",
        event_type=event_type,
        subject=subject,
        accumulator=accumulator,
        observations=(observation,),
        involved_track_ids=frozenset({42}),
        zone_ids=frozenset({"yard"}),
        promoted_monotonic_ns=last_ns,
        promoted_wall_utc=at,
    )
    return VerifiedEvent(
        candidate=candidate,
        verdicts=(ValidatorVerdict("camera_health", ValidatorOutcome.PASS, "camera is online"),),
        confidence_ratio=0.92,
        verified_monotonic_ns=last_ns,
    )


def _assessment(score: float = 61.9, severity: Severity = Severity.HIGH) -> SeverityAssessment:
    return SeverityAssessment(
        score=score,
        severity=severity,
        factors=(SeverityFactor("base", 0.65, 0.3, 0.195),),
        degraded=False,
    )


def _ulid(n: int) -> str:
    return UlidFactory(ManualClock(wall_start=WALL), seeded_rng(n, "cand")).new()


# ---------------------------------------------------------------- dedup


def test_one_situation_is_one_incident(manager: IncidentManager, store: Database) -> None:
    """Sixty frames of one person in one zone is one incident, not sixty."""
    first = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    assert first.created

    for n in range(2, 8):
        later = manager.record(
            _event(candidate_id=_ulid(n), at=WALL + timedelta(seconds=n)),
            _assessment(),
            CONTEXT,
        )
        assert not later.created
        assert later.extended
        assert later.incident_id == first.incident_id

    assert IncidentRepository(store).count(IncidentFilter()) == 1


def test_a_different_subject_is_a_different_incident(manager: IncidentManager) -> None:
    manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    other = manager.record(
        _event(candidate_id=_ulid(2), subject=SubjectKey("track", "43")),
        _assessment(),
        CONTEXT,
    )
    assert other.created


def test_a_different_event_type_is_a_different_incident(manager: IncidentManager) -> None:
    manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    other = manager.record(
        _event(candidate_id=_ulid(2), event_type=EventType.PERSON_DOWN),
        _assessment(),
        CONTEXT,
    )
    assert other.created


def test_the_dedup_key_is_the_identity_of_a_situation() -> None:
    assert dedup_key_text(CAMERA, EventType.INTRUSION, SUBJECT) == "gate-north|intrusion|track:42"


def test_extending_moves_last_observed_forward_only(
    manager: IncidentManager, store: Database
) -> None:
    """A replayed older event must not rewind an incident's span."""
    created = manager.record(
        _event(candidate_id=_ulid(1), at=WALL + timedelta(seconds=10), last_ns=11_000_000_000),
        _assessment(),
        CONTEXT,
    )
    repo = IncidentRepository(store)
    after_create = repo.lifecycle(created.incident_id)
    assert after_create is not None

    manager.record(
        _event(candidate_id=_ulid(2), at=WALL, last_ns=1_000_000_000),  # older
        _assessment(),
        CONTEXT,
    )
    after_replay = repo.lifecycle(created.incident_id)
    assert after_replay is not None
    assert after_replay.last_observed_wall_utc == after_create.last_observed_wall_utc


def test_extending_keeps_the_worst_severity_seen(manager: IncidentManager, store: Database) -> None:
    """An incident is as serious as its worst moment."""
    created = manager.record(
        _event(candidate_id=_ulid(1)), _assessment(40.0, Severity.MODERATE), CONTEXT
    )
    manager.record(
        _event(candidate_id=_ulid(2), at=WALL + timedelta(seconds=2)),
        _assessment(80.0, Severity.CRITICAL),
        CONTEXT,
    )
    lifecycle = IncidentRepository(store).lifecycle(created.incident_id)
    assert lifecycle is not None
    assert lifecycle.severity == int(Severity.CRITICAL)
    assert lifecycle.severity_score == pytest.approx(80.0)

    manager.record(
        _event(candidate_id=_ulid(3), at=WALL + timedelta(seconds=4)),
        _assessment(10.0, Severity.LOW),
        CONTEXT,
    )
    after = IncidentRepository(store).lifecycle(created.incident_id)
    assert after is not None
    assert after.severity == int(Severity.CRITICAL), "severity never walks back down"


def test_duplicate_delivery_of_the_same_decision_is_idempotent(
    manager: IncidentManager, store: Database
) -> None:
    """A response worker that retries after a transient failure must not create a second row."""
    repo = IncidentRepository(store)
    event = _event(candidate_id=_ulid(1))
    first = manager.record(event, _assessment(), CONTEXT)
    assert first.created
    # Close it so dedup does not simply extend, then replay the identical decision.
    manager.transition(first.incident_id, IncidentStatus.DISMISSED, actor="operator", reason="test")
    assert repo.count(IncidentFilter()) == 1

    incident = repo.get(first.incident_id)
    assert incident is not None
    replayed = repo.insert(
        incident,
        dedup_key=dedup_key_text(CAMERA, EventType.INTRUSION, SUBJECT),
        candidate_id=event.candidate.candidate_id,
        evidence_status="complete",
        now_wall_utc=WALL,
    )
    assert not replayed, "the insert is idempotent on the incident id"
    assert repo.count(IncidentFilter()) == 1


# ---------------------------------------------------------------- lifecycle


def test_an_incident_is_born_candidate_then_confirmed(
    manager: IncidentManager, store: Database
) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    transitions = IncidentRepository(store).transitions(created.incident_id)
    assert [t.to_status for t in transitions] == [
        IncidentStatus.CANDIDATE,
        IncidentStatus.CONFIRMED,
    ]
    assert all(t.actor == "system" for t in transitions)
    assert all(t.reason for t in transitions)


@pytest.mark.parametrize(
    ("to_status", "allowed"),
    [
        (IncidentStatus.ACTIVE, True),
        (IncidentStatus.RESOLVING, True),
        (IncidentStatus.DISMISSED, True),
        (IncidentStatus.SUPPRESSED, True),
        (IncidentStatus.RESOLVED, False),  # must pass through RESOLVING
        (IncidentStatus.CANDIDATE, False),  # no going back
        (IncidentStatus.EXPIRED, False),  # candidate timeout only
    ],
)
def test_only_defined_transitions_are_accepted(
    manager: IncidentManager, to_status: IncidentStatus, allowed: bool
) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    if allowed:
        assert (
            manager.transition(
                created.incident_id, to_status, actor="system", reason="test"
            ).to_status
            is to_status
        )
    else:
        with pytest.raises(TransitionRefused, match="cannot move to"):
            manager.transition(created.incident_id, to_status, actor="system", reason="test")


def test_a_terminal_state_is_terminal(manager: IncidentManager) -> None:
    """A resolved incident is never reopened; recurrence is a new incident (06 §9)."""
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    manager.transition(created.incident_id, IncidentStatus.ACTIVE, actor="operator", reason="ack")
    manager.transition(
        created.incident_id, IncidentStatus.RESOLVING, actor="system", reason="released"
    )
    manager.transition(
        created.incident_id, IncidentStatus.RESOLVED, actor="system", reason="sustained"
    )
    for target in IncidentStatus:
        with pytest.raises(TransitionRefused):
            manager.transition(created.incident_id, target, actor="operator", reason="no")


def test_release_walks_resolving_then_resolved(manager: IncidentManager, store: Database) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    released = manager.release(CAMERA, EventType.INTRUSION, SUBJECT, sustain_ms=2000.0)
    assert released == created.incident_id

    transitions = IncidentRepository(store).transitions(created.incident_id)
    assert [t.to_status for t in transitions][-2:] == [
        IncidentStatus.RESOLVING,
        IncidentStatus.RESOLVED,
    ]
    assert "2000 ms" in transitions[-1].reason
    lifecycle = IncidentRepository(store).lifecycle(created.incident_id)
    assert lifecycle is not None
    assert lifecycle.duration_ms is not None and lifecycle.duration_ms >= 0.0


def test_releasing_nothing_is_not_an_error(manager: IncidentManager) -> None:
    assert manager.release(CAMERA, EventType.FIRE, SUBJECT, sustain_ms=1.0) is None


def test_new_evidence_returns_a_releasing_incident_to_active(
    manager: IncidentManager, store: Database
) -> None:
    """The one non-monotonic edge in the lifecycle (06 §9)."""
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    manager.transition(
        created.incident_id, IncidentStatus.RESOLVING, actor="system", reason="released"
    )
    again = manager.record(
        _event(candidate_id=_ulid(2), at=WALL + timedelta(seconds=3)), _assessment(), CONTEXT
    )
    assert again.extended
    assert again.status is IncidentStatus.ACTIVE
    transitions = IncidentRepository(store).transitions(created.incident_id)
    assert transitions[-1].to_status is IncidentStatus.ACTIVE
    assert "new supporting evidence" in transitions[-1].reason


def test_a_resolved_timestamp_is_never_earlier_than_confirmed(
    manager: IncidentManager, store: Database
) -> None:
    """A backwards wall clock must not write a row the domain refuses to read back."""
    created = manager.record(
        _event(candidate_id=_ulid(1), at=WALL + timedelta(hours=1)), _assessment(), CONTEXT
    )
    manager.transition(
        created.incident_id,
        IncidentStatus.DISMISSED,
        actor="operator",
        reason="clock went backwards",
        at_wall_utc=WALL,  # an hour before `confirmed`
    )
    incident = IncidentRepository(store).get(created.incident_id)
    assert incident is not None, "the incident is still readable"
    assert incident.resolved_wall_utc == incident.confirmed_wall_utc
    assert incident.duration_ms == 0.0


def test_an_unknown_incident_cannot_be_transitioned(manager: IncidentManager) -> None:
    with pytest.raises(PersistenceError, match="no incident"):
        manager.transition(
            "01ARZ3NDEKTSV4RRFFQ69G5FAV", IncidentStatus.ACTIVE, actor="system", reason="x"
        )


# ---------------------------------------------------------------- operator actions


def test_acknowledge_dismiss_annotate_and_escalate(
    manager: IncidentManager, actions: OperatorActions, store: Database
) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    repo = IncidentRepository(store)

    assert actions.acknowledge(created.incident_id, note="on my way").actor == "operator"
    assert repo.lifecycle(created.incident_id).status is IncidentStatus.ACTIVE  # type: ignore[union-attr]

    assert actions.annotate(created.incident_id, "  spoke to the manager  ")
    incident = repo.get(created.incident_id)
    assert incident is not None and incident.operator_note == "spoke to the manager"

    assert actions.escalate(created.incident_id) is Severity.CRITICAL
    after = repo.lifecycle(created.incident_id)
    assert after is not None
    assert after.severity_score == pytest.approx(61.9), "the score is not overwritten"

    assert actions.dismiss(created.incident_id, reason="contractors").to_status is (
        IncidentStatus.DISMISSED
    )


def test_an_over_long_note_is_refused(manager: IncidentManager, actions: OperatorActions) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    with pytest.raises(PersistenceError, match="2000 characters"):
        actions.annotate(created.incident_id, "x" * 2001)


def test_a_whitespace_only_reason_falls_back_to_a_real_one(
    manager: IncidentManager, actions: OperatorActions
) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    transition = actions.dismiss(created.incident_id, reason="   ")
    assert transition.reason == "dismissed as a false positive"


def test_permissions_track_the_lifecycle(
    manager: IncidentManager, actions: OperatorActions
) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    before = actions.permissions(created.incident_id)
    assert before["acknowledge"][0] is True
    assert before["escalate"][0] is True

    actions.dismiss(created.incident_id, reason="done")
    after = actions.permissions(created.incident_id)
    assert after["acknowledge"][0] is False
    assert "closed" in (after["dismiss"][1] or "")
    assert after["annotate"][0] is True, "a note during review is still worth having"


def test_an_unknown_action_is_refused(manager: IncidentManager, actions: OperatorActions) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    allowed, reason = actions.can(created.incident_id, "launch_missiles")
    assert not allowed
    assert "unknown action" in (reason or "")


# ---------------------------------------------------------------- candidates


def test_every_candidate_outcome_is_recorded_including_rejections(store: Database) -> None:
    repo = CandidateRepository(store)
    verified = CandidateRecord(
        candidate=_event(candidate_id=_ulid(1)).candidate,
        outcome=CandidateOutcome.VERIFIED,
        verdicts=(ValidatorVerdict("camera_health", ValidatorOutcome.PASS, "online"),),
        accumulator_trace=(AccumulatorSample(1_000, 2.4, 0.91),),
        rejection_reason=None,
        decided_monotonic_ns=1_900_000_000,
        decided_wall_utc=WALL,
    )
    rejected = CandidateRecord(
        candidate=_event(candidate_id=_ulid(2), subject=SubjectKey("track", "7")).candidate,
        outcome=CandidateOutcome.REJECTED,
        verdicts=(
            ValidatorVerdict("scene_stability", ValidatorOutcome.REJECT, "the view is changing"),
        ),
        accumulator_trace=(),
        rejection_reason="scene_stability: the view is changing",
        decided_monotonic_ns=1_900_000_000,
        decided_wall_utc=WALL,
    )
    assert (
        repo.record_all([verified, rejected], incident_ids={verified.candidate.candidate_id: "i"})
        == 2
    )

    rows = repo.recent(limit=10)
    assert {r.outcome for r in rows} == {CandidateOutcome.VERIFIED, CandidateOutcome.REJECTED}
    assert repo.counts_by_outcome()[CandidateOutcome.REJECTED] == 1
    assert repo.rejection_reasons(limit=5)[0] == (
        "scene_stability: the view is changing",
        1,
    )
    only_rejected = repo.recent(limit=10, outcomes=(CandidateOutcome.REJECTED,))
    assert [r.candidate_id for r in only_rejected] == [rejected.candidate.candidate_id]


def test_a_deferred_candidate_is_updated_rather_than_duplicated(store: Database) -> None:
    """A candidate held in DEFER is recorded PENDING, then decided: one row, not two."""
    repo = CandidateRepository(store)
    candidate = _event(candidate_id=_ulid(1)).candidate
    pending = CandidateRecord(
        candidate=candidate,
        outcome=CandidateOutcome.PENDING,
        verdicts=(),
        accumulator_trace=(),
        rejection_reason=None,
        decided_monotonic_ns=1,
        decided_wall_utc=WALL,
    )
    repo.record_all([pending])
    decided = CandidateRecord(
        candidate=candidate,
        outcome=CandidateOutcome.EXPIRED,
        verdicts=(),
        accumulator_trace=(),
        rejection_reason="deferral_expired",
        decided_monotonic_ns=2,
        decided_wall_utc=WALL,
    )
    repo.record_all([decided])
    rows = repo.recent(limit=10)
    assert len(rows) == 1
    assert rows[0].outcome is CandidateOutcome.EXPIRED


# ---------------------------------------------------------------- retention


def _retention(store: Database, evidence_root: Path, clock: ManualClock) -> RetentionWorker:
    return RetentionWorker(
        repositories=RetentionRepositories(
            incidents=IncidentRepository(store),
            evidence=EvidenceRepository(store),
            candidates=CandidateRepository(store),
            metrics=MetricRepository(store),
            health=HealthEventRepository(store),
            mutes=MuteRepository(store),
        ),
        config=load_settings(
            env={}, use_dotenv=False, config_dir=Path("config")
        ).settings.retention,
        evidence_root=evidence_root,
        clock=clock,
        logger=get_logger("test.retention"),
        metrics=MetricsRegistry(),
    )


def test_retention_deletes_expired_evidence_files_and_rows(
    store: Database, tmp_path: Path, manager: IncidentManager
) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    root = tmp_path / "evidence"
    relative = f"{CAMERA}/2026/03/01/{created.incident_id}"
    (root / relative).mkdir(parents=True)
    path = root / relative / "snapshot_raw.jpg"
    path.write_bytes(b"x" * 100)
    EvidenceRepository(store).add_all(
        [
            Evidence(
                evidence_id=_ulid(2),
                incident_id=created.incident_id,
                kind=EvidenceKind.SNAPSHOT_RAW,
                relative_path=f"{relative}/snapshot_raw.jpg",
                sha256="0" * 64,
                size_bytes=100,
                media_type="image/jpeg",
                captured_wall_utc=WALL,
                span_ms=None,
                expires_wall_utc=WALL - timedelta(days=1),  # already expired
            )
        ]
    )

    report = _retention(store, root, ManualClock(wall_start=WALL)).sweep()
    assert report.evidence_files_deleted == 1
    assert report.evidence_rows_deleted == 1
    assert report.evidence_bytes_reclaimed == 100
    assert not path.exists()
    assert EvidenceRepository(store).for_incident(created.incident_id) == ()
    assert not report.failures


def test_retention_never_deletes_an_open_incident(
    store: Database, tmp_path: Path, manager: IncidentManager
) -> None:
    manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    clock = ManualClock(wall_start=WALL + timedelta(days=4000))
    report = _retention(store, tmp_path / "evidence", clock).sweep()
    assert report.incidents_deleted == 0
    assert IncidentRepository(store).count(IncidentFilter()) == 1


def test_retention_never_deletes_an_annotated_incident(
    store: Database, manager: IncidentManager, actions: OperatorActions, tmp_path: Path
) -> None:
    """An annotated incident is a human artifact (05 §8)."""
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    actions.dismiss(created.incident_id, reason="false positive")
    actions.annotate(created.incident_id, "checked the footage myself")

    clock = ManualClock(wall_start=WALL + timedelta(days=4000))
    report = _retention(store, tmp_path / "evidence", clock).sweep()
    assert report.incidents_deleted == 0


def test_retention_deletes_a_closed_unannotated_incident_with_no_evidence(
    store: Database, manager: IncidentManager, actions: OperatorActions, tmp_path: Path
) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    actions.dismiss(created.incident_id, reason="false positive")
    clock = ManualClock(wall_start=WALL + timedelta(days=4000))
    report = _retention(store, tmp_path / "evidence", clock).sweep()
    assert report.incidents_deleted == 1
    assert IncidentRepository(store).get(created.incident_id) is None


def test_an_incident_is_never_orphaned_from_its_evidence(
    store: Database, manager: IncidentManager, actions: OperatorActions, tmp_path: Path
) -> None:
    """Evidence that has not expired keeps its incident alive, however old (05 §8)."""
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    actions.dismiss(created.incident_id, reason="done")
    root = tmp_path / "evidence"
    relative = f"{CAMERA}/2026/03/01/{created.incident_id}"
    (root / relative).mkdir(parents=True)
    (root / relative / "explanation.json").write_bytes(b"{}")
    EvidenceRepository(store).add_all(
        [
            Evidence(
                evidence_id=_ulid(3),
                incident_id=created.incident_id,
                kind=EvidenceKind.EXPLANATION,
                relative_path=f"{relative}/explanation.json",
                sha256="0" * 64,
                size_bytes=2,
                media_type="application/json",
                captured_wall_utc=WALL,
                span_ms=None,
                expires_wall_utc=WALL + timedelta(days=100_000),  # not yet expired
            )
        ]
    )
    clock = ManualClock(wall_start=WALL + timedelta(days=4000))
    report = _retention(store, root, clock).sweep()
    assert report.incidents_deleted == 0
    assert IncidentRepository(store).get(created.incident_id) is not None


def test_retention_collects_an_interrupted_write(store: Database, tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    directory = root / CAMERA / "2026" / "03" / "01"
    directory.mkdir(parents=True)
    orphan = directory / "clip_pre.mp4.part"
    orphan.write_bytes(b"half a clip")
    import os

    stale = (WALL - timedelta(days=1)).timestamp()
    os.utime(orphan, (stale, stale))

    report = _retention(store, root, ManualClock(wall_start=WALL)).sweep()
    assert report.orphan_parts_deleted == 1
    assert not orphan.exists()


def test_a_sweep_that_deletes_nothing_still_reports(store: Database, tmp_path: Path) -> None:
    """Silence is indistinguishable from a broken worker (05 §8)."""
    report = _retention(store, tmp_path / "evidence", ManualClock(wall_start=WALL)).sweep()
    assert not report.deleted_anything
    assert report.as_fields()["incidents"] == 0
    assert not report.failures


def test_a_missing_evidence_file_is_counted_not_fatal(
    store: Database, tmp_path: Path, manager: IncidentManager
) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    EvidenceRepository(store).add_all(
        [
            Evidence(
                evidence_id=_ulid(4),
                incident_id=created.incident_id,
                kind=EvidenceKind.CLIP_PRE,
                relative_path=f"{CAMERA}/2026/03/01/{created.incident_id}/clip_pre.mp4",
                sha256="0" * 64,
                size_bytes=10,
                media_type="video/mp4",
                captured_wall_utc=WALL,
                span_ms=None,
                expires_wall_utc=WALL - timedelta(days=1),
            )
        ]
    )
    report = _retention(store, tmp_path / "evidence", ManualClock(wall_start=WALL)).sweep()
    assert report.evidence_files_missing == 1
    assert report.evidence_rows_deleted == 1
    assert not report.failures


def test_the_evidence_status_round_trips(store: Database, manager: IncidentManager) -> None:
    created = manager.record(_event(candidate_id=_ulid(1)), _assessment(), CONTEXT)
    repo = IncidentRepository(store)
    assert repo.evidence_status(created.incident_id) == EvidenceStatus.PENDING.value
    assert manager.set_evidence_status(created.incident_id, EvidenceStatus.PARTIAL)
    assert repo.evidence_status(created.incident_id) == EvidenceStatus.PARTIAL.value
