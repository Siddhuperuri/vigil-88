"""Dedup identity, cooldown, rate limiting (06 §6) and arbitration (06 §7)."""

from __future__ import annotations

import pytest

from tests.support.candidates import CAMERA, candidate, verified
from vigil.config.schema.modules import ArbitrationConfig, VerificationConfig
from vigil.core.units import ms_to_ns
from vigil.domain.enums import EventType
from vigil.domain.observation import SubjectKey
from vigil.verification.arbitration import SUBSUMED_BY, arbitrate
from vigil.verification.ledger import SuppressionLedger, dedup_key

TRACK_1 = SubjectKey("track", "1")
TRACK_2 = SubjectKey("track", "2")
KEY = dedup_key(CAMERA, EventType.INTRUSION, TRACK_1)


def ledger(**overrides: object) -> SuppressionLedger:
    return SuppressionLedger(VerificationConfig(**overrides))  # type: ignore[arg-type]


# ------------------------------------------------------------------ dedup identity (§11)


def test_the_dedup_key_is_camera_type_and_subject() -> None:
    assert dedup_key(CAMERA, EventType.INTRUSION, TRACK_1) == (
        CAMERA,
        EventType.INTRUSION,
        TRACK_1,
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            dedup_key("cam-01", EventType.INTRUSION, TRACK_1),
            dedup_key("cam-02", EventType.INTRUSION, TRACK_1),
        ),
        (
            dedup_key(CAMERA, EventType.INTRUSION, TRACK_1),
            dedup_key(CAMERA, EventType.FIRE, TRACK_1),
        ),
        (
            dedup_key(CAMERA, EventType.INTRUSION, TRACK_1),
            dedup_key(CAMERA, EventType.INTRUSION, TRACK_2),
        ),
    ],
)
def test_unrelated_situations_never_share_a_key(a: object, b: object) -> None:
    """CAM-01/Person-12, CAM-01/Person-19 and CAM-02/Person-04 stay logically independent."""
    assert a != b


# ------------------------------------------------------------------ open events


def test_opening_an_event_makes_it_findable() -> None:
    led = ledger()
    event = led.record_open(KEY, candidate_id="abc", t_monotonic_ns=0)
    assert led.is_open(KEY)
    assert led.open_event(KEY) is event
    assert len(led) == 1


def test_opening_the_same_key_twice_extends_rather_than_duplicates() -> None:
    """The same persistent situation must not produce a second event (06 §6 validator 7)."""
    led = ledger()
    first = led.record_open(KEY, candidate_id="abc", t_monotonic_ns=0)
    second = led.record_open(KEY, candidate_id="def", t_monotonic_ns=ms_to_ns(5000.0))
    assert first is second
    assert len(led) == 1
    assert second.candidate_id == "abc"  # the original event, extended
    assert second.extensions == 1


def test_extending_an_open_event_advances_its_last_observation() -> None:
    led = ledger()
    led.record_open(KEY, candidate_id="abc", t_monotonic_ns=0)
    event = led.extend_open(KEY, ms_to_ns(9000.0))
    assert event is not None
    assert event.duration_ms(ms_to_ns(9000.0)) == pytest.approx(9000.0)


def test_extending_an_unknown_key_is_a_no_op() -> None:
    assert ledger().extend_open(KEY, 0) is None


def test_open_keys_are_deterministic() -> None:
    led = ledger()
    for subject in (TRACK_2, TRACK_1):
        led.record_open(
            dedup_key(CAMERA, EventType.INTRUSION, subject), candidate_id="x", t_monotonic_ns=0
        )
    assert led.open_keys() == (
        dedup_key(CAMERA, EventType.INTRUSION, TRACK_1),
        dedup_key(CAMERA, EventType.INTRUSION, TRACK_2),
    )


def test_closing_an_event_removes_it_and_returns_it() -> None:
    led = ledger()
    led.record_open(KEY, candidate_id="abc", t_monotonic_ns=0)
    closed = led.close(KEY, ms_to_ns(5000.0))
    assert closed is not None and closed.candidate_id == "abc"
    assert not led.is_open(KEY)


# ------------------------------------------------------------------ cooldown


def test_a_closed_event_starts_a_cooldown() -> None:
    led = ledger(cooldown_ms=60_000)
    led.close(KEY, ms_to_ns(1000.0))
    assert led.in_cooldown(KEY, ms_to_ns(2000.0))
    assert led.cooldown_remaining_ms(KEY, ms_to_ns(2000.0)) == pytest.approx(59_000.0)


def test_the_cooldown_expires() -> None:
    led = ledger(cooldown_ms=5000)
    led.close(KEY, ms_to_ns(1000.0))
    assert not led.in_cooldown(KEY, ms_to_ns(6000.0))
    assert led.cooldown_remaining_ms(KEY, ms_to_ns(6000.0)) == 0.0


def test_a_key_that_never_closed_is_not_in_cooldown() -> None:
    assert not ledger().in_cooldown(KEY, ms_to_ns(1000.0))


# ------------------------------------------------------------------ rate limit


def test_recent_count_counts_only_this_camera() -> None:
    led = ledger()
    led.record_open(
        dedup_key("cam-01", EventType.INTRUSION, TRACK_1), candidate_id="a", t_monotonic_ns=0
    )
    led.record_open(
        dedup_key("cam-02", EventType.INTRUSION, TRACK_1), candidate_id="b", t_monotonic_ns=0
    )
    assert led.recent_count("cam-01", 0) == 1
    assert led.recent_count("cam-02", 0) == 1
    assert led.recent_count("cam-03", 0) == 0


def test_the_rate_window_slides() -> None:
    led = ledger(rate_limit_window_ms=1000.0)
    led.record_open(KEY, candidate_id="a", t_monotonic_ns=0)
    assert led.recent_count(CAMERA, ms_to_ns(500.0)) == 1
    assert led.recent_count(CAMERA, ms_to_ns(1500.0)) == 0


def test_extending_an_event_does_not_count_again_toward_the_rate_limit() -> None:
    """One situation is one event however long it lasts."""
    led = ledger()
    for t in (0.0, 1000.0, 2000.0):
        led.record_open(KEY, candidate_id="a", t_monotonic_ns=ms_to_ns(t))
    assert led.recent_count(CAMERA, ms_to_ns(2000.0)) == 1


# ------------------------------------------------------------------ pruning and reset


def test_pruning_drops_expired_cooldowns() -> None:
    """Without this the ledger grows one entry per subject the system has ever seen."""
    led = ledger(cooldown_ms=1000)
    led.close(KEY, ms_to_ns(0.0))
    assert led.closed_at(KEY) is not None
    led.prune(ms_to_ns(5000.0))
    assert led.closed_at(KEY) is None


def test_pruning_keeps_a_live_cooldown() -> None:
    led = ledger(cooldown_ms=60_000)
    led.close(KEY, ms_to_ns(0.0))
    led.prune(ms_to_ns(1000.0))
    assert led.closed_at(KEY) is not None


def test_pruning_does_not_close_open_events() -> None:
    led = ledger()
    led.record_open(KEY, candidate_id="a", t_monotonic_ns=0)
    led.prune(ms_to_ns(10_000_000.0))
    assert led.is_open(KEY)


def test_resetting_a_camera_forgets_only_that_camera() -> None:
    led = ledger()
    mine = dedup_key("cam-01", EventType.INTRUSION, TRACK_1)
    theirs = dedup_key("cam-02", EventType.INTRUSION, TRACK_1)
    led.record_open(mine, candidate_id="a", t_monotonic_ns=0)
    led.record_open(theirs, candidate_id="b", t_monotonic_ns=0)
    led.close(mine, ms_to_ns(100.0))
    led.reset_camera("cam-01")
    assert led.closed_at(mine) is None
    assert led.is_open(theirs)
    assert led.recent_count("cam-01", 0) == 0
    assert led.recent_count("cam-02", 0) == 1


# ------------------------------------------------------------------ arbitration (§7)


CONFIG = ArbitrationConfig()


def test_arbitration_leaves_an_unrelated_event_alone() -> None:
    outcomes = arbitrate([verified()], config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0))
    assert len(outcomes) == 1
    assert not outcomes[0].suppressed
    assert outcomes[0].notes == ()


def test_a_specific_event_inside_a_broader_one_is_subsumed() -> None:
    """Two events for one situation is a reporting failure; the specific one becomes a
    transition on the broader (06 §7)."""
    assert SUBSUMED_BY[EventType.RESTRICTED_AREA_ENTRY] is EventType.INTRUSION
    broad = verified(cand=candidate(event_type=EventType.INTRUSION, subject=TRACK_1))
    narrow = verified(cand=candidate(event_type=EventType.RESTRICTED_AREA_ENTRY, subject=TRACK_1))
    outcomes = arbitrate([broad, narrow], config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0))
    by_type = {o.event.candidate.event_type: o for o in outcomes}
    assert by_type[EventType.RESTRICTED_AREA_ENTRY].suppressed
    assert by_type[EventType.RESTRICTED_AREA_ENTRY].suppressed_by == broad.candidate.candidate_id
    assert not by_type[EventType.INTRUSION].suppressed


def test_subsumption_requires_the_same_subject() -> None:
    broad = verified(cand=candidate(event_type=EventType.INTRUSION, subject=TRACK_1))
    narrow = verified(cand=candidate(event_type=EventType.RESTRICTED_AREA_ENTRY, subject=TRACK_2))
    outcomes = arbitrate([broad, narrow], config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0))
    assert not any(o.suppressed for o in outcomes)


def test_subsumption_requires_the_same_camera() -> None:
    broad = verified(
        cand=candidate(event_type=EventType.INTRUSION, subject=TRACK_1, camera_id="cam-01")
    )
    narrow = verified(
        cand=candidate(
            event_type=EventType.RESTRICTED_AREA_ENTRY, subject=TRACK_1, camera_id="cam-02"
        )
    )
    outcomes = arbitrate([broad, narrow], config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0))
    assert not any(o.suppressed for o in outcomes)


def test_related_events_raise_the_escalation_count() -> None:
    events = [verified(cand=candidate(subject=SubjectKey("track", str(i)))) for i in range(4)]
    outcomes = arbitrate(events, config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0))
    assert all(o.escalation_count == 4 for o in outcomes)
    assert all(o.notes for o in outcomes)  # the note explains why it escalated


def test_escalation_counts_only_inside_the_window() -> None:
    config = ArbitrationConfig(escalation_window_ms=500.0)
    events = [verified(cand=candidate(subject=SubjectKey("track", str(i)))) for i in range(4)]
    outcomes = arbitrate(events, config=config, now_monotonic_ns=ms_to_ns(60_000.0))
    assert all(o.escalation_count == 0 for o in outcomes)


def test_escalation_counts_only_this_camera_and_type() -> None:
    mine = verified(cand=candidate(camera_id="cam-01", subject=TRACK_1))
    elsewhere = verified(cand=candidate(camera_id="cam-02", subject=TRACK_2))
    other_type = verified(
        cand=candidate(camera_id="cam-01", event_type=EventType.FIRE, subject=TRACK_2)
    )
    outcomes = arbitrate(
        [mine, elsewhere, other_type], config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0)
    )
    for outcome in outcomes:
        assert outcome.escalation_count == 1


def test_arbitration_is_deterministic() -> None:
    events = [verified(cand=candidate(subject=SubjectKey("track", str(i)))) for i in (3, 1, 2)]
    first = arbitrate(events, config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0))
    second = arbitrate(list(reversed(events)), config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0))
    assert [str(o.event.candidate.subject) for o in first] == [
        str(o.event.candidate.subject) for o in second
    ]


def test_arbitration_never_promotes_anything() -> None:
    """A second promotion path would defeat the whole model (06 §7): arbitration counts
    escalation for the severity engine, it does not create a CROWD_ANOMALY candidate."""
    events = [verified(cand=candidate(subject=SubjectKey("track", str(i)))) for i in range(6)]
    outcomes = arbitrate(events, config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0))
    assert len(outcomes) == len(events)
    assert {o.event.candidate.event_type for o in outcomes} == {EventType.INTRUSION}


def test_arbitration_considers_already_active_events() -> None:
    active = verified(cand=candidate(event_type=EventType.INTRUSION, subject=TRACK_1))
    new = verified(cand=candidate(event_type=EventType.RESTRICTED_AREA_ENTRY, subject=TRACK_1))
    outcomes = arbitrate([new], config=CONFIG, now_monotonic_ns=ms_to_ns(1200.0), active=[active])
    assert outcomes[0].suppressed
    assert outcomes[0].suppressed_by == active.candidate.candidate_id


def test_arbitration_of_nothing_is_nothing() -> None:
    assert arbitrate([], config=CONFIG, now_monotonic_ns=0) == ()
