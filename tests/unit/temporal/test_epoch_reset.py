"""What a stream epoch change does to accumulators (07 §5).

07 §5's table calls this "the subtle one, and it is the kind of detail that decides whether a
system is trustworthy across real-world network conditions". The implementation discarded every
accumulator until P6, which no shipped module could notice because `intrusion`, `person_down`
and `abandoned_object` all use `track:` subjects — and which would have been silently wrong for
the first module that did not.
"""

from __future__ import annotations

from vigil.config.schema.modules import TemporalConfig
from vigil.core.units import ms_to_ns
from vigil.domain.observation import Observation, SubjectKey
from vigil.temporal.store import AccumulatorStore

CAMERA = "cam-a"


def store(**overrides: object) -> AccumulatorStore:
    return AccumulatorStore(
        camera_id=CAMERA,
        config=TemporalConfig(**overrides),  # type: ignore[arg-type]
        support_slot_ms=100.0,
    )


def observe(s: AccumulatorStore, subject: SubjectKey, *, at_ms: float, signal: float = 0.9) -> None:
    s.observe(
        Observation(
            camera_id=CAMERA,
            module_id="test",
            subject=subject,
            signal=signal,
            t_monotonic_ns=ms_to_ns(at_ms),
            facts={},
        )
    )


TRACK = SubjectKey("track", "7")
ZONE = SubjectKey("zone", "yard")
REGION = SubjectKey("region", "3,4")
WHOLE_CAMERA = SubjectKey("camera", CAMERA)


def test_a_track_accumulator_is_discarded_and_a_place_accumulator_is_kept() -> None:
    """Track 7 before the gap is not track 7 after it; the yard is still the yard."""
    s = store()
    for subject in (TRACK, ZONE, REGION, WHOLE_CAMERA):
        observe(s, subject, at_ms=0.0)
    assert len(s) == 4

    dropped = s.reset_tracks()

    assert [key[1] for key in dropped] == [TRACK]
    survivors = s.keys()  # AccumulatorStore.keys(), not a dict
    remaining = {key[1] for key in survivors}
    assert remaining == {ZONE, REGION, WHOLE_CAMERA}


def test_several_tracks_go_and_nothing_else_does() -> None:
    s = store()
    for i in range(5):
        observe(s, SubjectKey("track", str(i)), at_ms=0.0)
    observe(s, ZONE, at_ms=0.0)
    assert len(s.reset_tracks()) == 5
    survivors = s.keys()  # AccumulatorStore.keys(), not a dict
    assert {key[1] for key in survivors} == {ZONE}


def test_a_kept_accumulator_decays_across_the_gap_rather_than_surviving_intact() -> None:
    """ "A fire does not stop burning because the network blipped" — but nor does it burn just as
    brightly after two minutes of no evidence. Decay needs no extra work: the accumulator
    decays by frame timestamps, so the first observation after the gap applies every elapsed
    half-life by itself. This asserts that it genuinely happens."""
    s = store(half_life_ms=1000.0)
    for at in (0.0, 100.0, 200.0, 300.0):
        observe(s, ZONE, at_ms=at)
    zone_accumulator = s.get("test", ZONE)
    assert zone_accumulator is not None
    before = zone_accumulator.state_at(ms_to_ns(300.0))
    assert before.log_odds > 0.0

    s.reset_tracks()  # the epoch change

    # 10 s of silence is ten half-lives. `decay_all_to` is what the analysis tick calls on every
    # frame, including the first frame after the gap, so this is the production path.
    s.decay_all_to(ms_to_ns(10_300.0))
    kept = s.get("test", ZONE)
    assert kept is not None
    after = kept.state_at(ms_to_ns(10_300.0))
    assert 0.0 < after.log_odds < before.log_odds / 100.0
    # Kept, not revived: the evidence is still there, just worth far less than it was.
    assert after.observation_count == before.observation_count


def test_resetting_tracks_on_an_empty_store_is_a_no_op() -> None:
    assert store().reset_tracks() == ()


def test_reset_still_forgets_everything_when_that_is_what_is_wanted() -> None:
    """`reset()` is kept for the cases that really do mean all of it, such as a reconfiguration."""
    s = store()
    observe(s, TRACK, at_ms=0.0)
    observe(s, ZONE, at_ms=0.0)
    s.reset()
    assert len(s) == 0
