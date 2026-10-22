"""abandoned_object: the difference between stationary and abandoned.

The tests that matter most here are the ones asserting **silence**. A module that fired on
"stationary object" would fire on every bin on every camera forever, so most of this file is
about what the module declines to say.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from tests.support.scenes import FULL_CAPABILITIES, SceneStateBuilder
from vigil.core.capabilities import Capability
from vigil.core.protocols.module import ModuleContext
from vigil.domain.enums import EventType, ObjectClass, TrackFlag, ZoneKind
from vigil.domain.observation import Observation
from vigil.events.builtin.abandoned_object import (
    MODULE,
    AbandonedObjectModule,
    AbandonedObjectParams,
    create,
)
from vigil.observability.logging import get_logger

CAMERA = "test-cam"
BAG = (40.0, 30.0)
# The bag sits inside the default zone polygon (the lower-right quadrant).
BAG_AT = (0.7, 0.6)
BESIDE = (0.71, 0.61)
# About one bag-height away: an owner under the default radius, not under a tight one.
NEARBY = (0.76, 0.6)
FAR = (0.1, 0.9)
SEPARATION_MS = 10_000.0


def module(**params: object) -> AbandonedObjectModule:
    built = create()
    built.bind(
        ModuleContext(
            camera_id=CAMERA,
            params={**MODULE.default_params, **params},
            logger=get_logger("test.abandoned"),
        )
    )
    return built


def scene(
    *,
    t_ms: float,
    bag_flags: Sequence[TrackFlag] = (TrackFlag.STATIONARY,),
    bag_stationary_since_ms: float | None = 0.0,
    person_at: tuple[float, float] | None = None,
    bag_hits: int = 20,
    capabilities: frozenset[Capability] = FULL_CAPABILITIES,
    zone_kind: ZoneKind = ZoneKind.RESTRICTED,
    bag_class: ObjectClass = ObjectClass.OBJECT_BAG,
    person_visible: bool = True,
) -> object:
    builder = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_capabilities(capabilities)
        .with_zone("dock", zone_kind, criticality=2)
    )
    builder.t_ms = t_ms
    builder.with_track(
        1,
        bag_class,
        at=BAG_AT,
        size=BAG,
        hits=bag_hits,
        age_frames=bag_hits,
        in_zones=["dock"],
        flags=bag_flags,
        stationary_since_ms=bag_stationary_since_ms,
        first_seen_ms=0.0,
    )
    if person_at is not None:
        builder.with_track(
            2,
            ObjectClass.PERSON,
            at=person_at,
            size=(50.0, 150.0),
            hits=10,
            age_frames=10,
            in_zones=["dock"] if person_at in (BESIDE, NEARBY) else [],
            time_since_update_frames=0 if person_visible else 3,
            first_seen_ms=0.0,
        )
    return builder.build()


def only(emitted: Sequence[Observation]) -> Observation:
    assert len(emitted) == 1, f"expected one observation, got {len(emitted)}"
    return emitted[0]


def abandoned(instance: AbandonedObjectModule, **overrides: object) -> Observation:
    """Walk the module through owner-present then owner-gone, and return the final observation."""
    instance.observe(scene(t_ms=0.0, person_at=BESIDE, **overrides))  # type: ignore[arg-type]
    return only(
        instance.observe(
            scene(t_ms=SEPARATION_MS + 1000.0, person_at=None, **overrides)  # type: ignore[arg-type]
        )
    )


# ---------------------------------------------------------------- the descriptor


def test_the_descriptor_is_honest_about_what_it_reports() -> None:
    assert MODULE.event_type is EventType.ABANDONED_OBJECT
    assert MODULE.requires == {Capability.DETECTION, Capability.TRACKING}
    assert MODULE.optional == {Capability.ZONES}
    text = MODULE.description.lower()
    assert "state, not an act" in text or "not an act" in text
    assert "never saw an owner" in text


# ---------------------------------------------------------------- silence


def test_furniture_is_never_reported() -> None:
    """An object nobody has been seen beside is furniture as far as this module can tell."""
    instance = module()
    for step in range(30):
        assert instance.observe(scene(t_ms=step * 1000.0, person_at=None)) == ()
    assert instance.observe(scene(t_ms=60_000.0, person_at=None)) == ()


def test_a_moving_object_is_being_carried() -> None:
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    emitted = instance.observe(
        scene(
            t_ms=SEPARATION_MS + 1000.0, person_at=None, bag_flags=(), bag_stationary_since_ms=None
        )
    )
    assert emitted == ()


def test_an_object_whose_owner_just_stepped_away_is_not_abandoned() -> None:
    """Setting a bag down and standing up is not abandoning it."""
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    emitted = instance.observe(scene(t_ms=2000.0, person_at=None))
    assert emitted == ()


def test_a_non_watched_class_is_ignored() -> None:
    instance = module()
    assert (
        instance.observe(scene(t_ms=0.0, person_at=BESIDE, bag_class=ObjectClass.VEHICLE_CAR)) == ()
    )


def test_a_low_quality_track_is_ignored() -> None:
    assert module().observe(scene(t_ms=0.0, person_at=BESIDE, bag_hits=2)) == ()


def test_an_empty_scene_says_nothing() -> None:
    assert module().observe(SceneStateBuilder(camera_id=CAMERA).build()) == ()


# ---------------------------------------------------------------- the main path


def test_a_separated_stationary_object_is_reported() -> None:
    emitted = abandoned(module())
    assert emitted.signal > 0.5
    assert str(emitted.subject) == "track:1"
    assert emitted.facts["separation_ms"] >= SEPARATION_MS
    assert emitted.facts["separation_bonus"] == 0.15
    assert emitted.facts["stationary"] is True


def test_persistence_adds_evidence_once_it_has_been_there_long_enough() -> None:
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    soon = only(instance.observe(scene(t_ms=15_000.0, person_at=None)))
    later = only(instance.observe(scene(t_ms=90_000.0, person_at=None)))
    assert "persistence_bonus" not in soon.facts
    assert later.facts["persistence_bonus"] == 0.15
    assert later.signal > soon.signal


def test_the_signal_is_capped() -> None:
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    emitted = only(instance.observe(scene(t_ms=600_000.0, person_at=None)))
    assert emitted.signal <= 0.95


# ---------------------------------------------------------------- ownership


def test_an_attended_object_is_evidence_against() -> None:
    """What releases an event when someone comes back for their bag."""
    emitted = only(module().observe(scene(t_ms=0.0, person_at=BESIDE)))
    assert emitted.signal < 0.5
    assert emitted.facts["attended"] is True
    assert emitted.facts["owner_track_id"] == 2.0
    assert emitted.facts["owner_distance_ratio"] == pytest.approx(0.0, abs=0.5)


def test_an_owner_returning_flips_the_evidence() -> None:
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    gone = only(instance.observe(scene(t_ms=20_000.0, person_at=None)))
    returned = only(instance.observe(scene(t_ms=21_000.0, person_at=BESIDE)))
    assert gone.signal > 0.5
    assert returned.signal < 0.5


def test_a_distant_person_is_not_an_owner() -> None:
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    emitted = only(instance.observe(scene(t_ms=20_000.0, person_at=FAR)))
    assert emitted.signal > 0.5
    assert "owner_track_id" not in emitted.facts


def test_only_a_visible_owner_counts() -> None:
    """A predicted box is the filter's guess: "someone might be standing there" is not evidence
    that the bag is attended."""
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    emitted = only(instance.observe(scene(t_ms=20_000.0, person_at=BESIDE, person_visible=False)))
    assert emitted.signal > 0.5, "an occluded person does not attend the bag"


def test_the_owner_radius_is_relative_to_the_object_s_own_box() -> None:
    """One threshold has to mean the same thing for a bag near the camera and one far away.

    `NEARBY` is about one bag-height away: inside the default 2.5 radius, outside a tight one.
    """
    generous = only(module().observe(scene(t_ms=0.0, person_at=NEARBY)))
    assert generous.facts["attended"] is True
    assert generous.facts["owner_distance_ratio"] == pytest.approx(1.06, abs=0.2)

    # Under a tight radius the same person is not an owner, so no ownership is ever recorded
    # and the object reads as furniture: silence, not a weak signal.
    assert module(owner_distance_ratio=0.2).observe(scene(t_ms=0.0, person_at=NEARBY)) == ()


# ---------------------------------------------------------------- zones


def test_with_zones_only_watched_kinds_are_considered() -> None:
    instance = module()
    emitted = instance.observe(scene(t_ms=0.0, person_at=BESIDE, zone_kind=ZoneKind.CROSSING_LINE))
    assert emitted == (), "a zone kind outside zone_kinds is not watched"


def test_without_zones_the_whole_frame_is_watched_and_the_gap_is_named() -> None:
    reduced = FULL_CAPABILITIES - {Capability.ZONES}
    instance = module()
    emitted = only(
        instance.observe(
            scene(
                t_ms=0.0,
                person_at=BESIDE,
                zone_kind=ZoneKind.CROSSING_LINE,
                capabilities=reduced,
            )
        )
    )
    assert emitted.facts["zones_unavailable"] is True
    assert "zone_id" not in emitted.facts


def test_with_zones_the_location_is_quoted() -> None:
    emitted = only(module().observe(scene(t_ms=0.0, person_at=BESIDE)))
    assert emitted.facts["zone_id"] == "dock"


# ---------------------------------------------------------------- state hygiene


def test_ownership_history_is_cleared_on_an_epoch_change() -> None:
    """Track ids no longer mean what they meant, so ownership keyed by them is worthless."""
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    after = scene(t_ms=20_000.0, person_at=None)
    from dataclasses import replace

    bumped = replace(after, frame_meta=replace(after.frame_meta, stream_epoch=2))  # type: ignore[attr-defined]
    assert instance.observe(bumped) == ()


def test_state_is_pruned_to_the_tracks_actually_present() -> None:
    instance = module()
    for index in range(300):
        builder = SceneStateBuilder(camera_id=CAMERA).with_zone("dock", ZoneKind.RESTRICTED)
        builder.t_ms = index * 100.0
        builder.with_track(
            index,  # a brand-new identity every frame
            ObjectClass.OBJECT_BAG,
            at=BAG_AT,
            size=BAG,
            hits=20,
            age_frames=20,
            in_zones=["dock"],
            flags=[TrackFlag.STATIONARY],
            stationary_since_ms=0.0,
            first_seen_ms=0.0,
        )
        builder.with_track(
            index + 100_000,
            ObjectClass.PERSON,
            at=BESIDE,
            hits=10,
            age_frames=10,
            first_seen_ms=0.0,
        )
        instance.observe(builder.build())
    held = instance._owner_seen_ns
    assert len(held) <= 2, f"ownership history grew to {len(held)} entries over 300 frames"


def test_observing_the_same_scene_twice_says_the_same_thing() -> None:
    """Every state update writes an absolute instant, so repetition is idempotent."""
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    built = scene(t_ms=20_000.0, person_at=None)
    assert tuple(instance.observe(built)) == tuple(instance.observe(built))


def test_release_then_bind_forgets_everything() -> None:
    instance = module()
    instance.observe(scene(t_ms=0.0, person_at=BESIDE))
    instance.release()
    instance.bind(
        ModuleContext(
            camera_id=CAMERA,
            params=MODULE.default_params,
            logger=get_logger("test.abandoned"),
        )
    )
    assert instance.observe(scene(t_ms=20_000.0, person_at=None)) == ()


def test_the_number_of_objects_per_frame_is_bounded() -> None:
    instance = module(max_objects=3)
    builder = SceneStateBuilder(camera_id=CAMERA).with_zone("dock", ZoneKind.RESTRICTED)
    builder.t_ms = 0.0
    for index in range(1, 11):
        builder.with_track(
            index,
            ObjectClass.OBJECT_BAG,
            at=(0.6 + index * 0.01, 0.6),
            size=BAG,
            hits=20,
            age_frames=20,
            in_zones=["dock"],
            flags=[TrackFlag.STATIONARY],
            stationary_since_ms=0.0,
            first_seen_ms=0.0,
        )
    builder.with_track(99, ObjectClass.PERSON, at=BESIDE, hits=10, age_frames=10, first_seen_ms=0.0)
    emitted = instance.observe(builder.build())
    assert len(emitted) <= 3


# ---------------------------------------------------------------- parameters


def test_an_exclusion_zone_cannot_be_watched() -> None:
    with pytest.raises(ValidationError, match="invert the meaning"):
        AbandonedObjectParams(zone_kinds=(ZoneKind.EXCLUSION,))


def test_a_class_cannot_own_itself() -> None:
    with pytest.raises(ValidationError, match="attend itself"):
        AbandonedObjectParams(
            object_classes=(ObjectClass.OBJECT_BAG,), owner_classes=(ObjectClass.OBJECT_BAG,)
        )


def test_an_empty_owner_class_list_is_refused() -> None:
    """With no owner class every object is permanently unattended."""
    with pytest.raises(ValidationError, match="fire on the furniture"):
        AbandonedObjectParams(owner_classes=())


def test_an_empty_object_class_list_is_refused() -> None:
    with pytest.raises(ValidationError, match="never fire"):
        AbandonedObjectParams(object_classes=())


def test_an_unknown_parameter_is_an_error() -> None:
    with pytest.raises(ValidationError):
        AbandonedObjectParams(seperation_after_ms=1.0)  # type: ignore[call-arg]


def test_a_longer_separation_window_delays_the_report() -> None:
    patient = module(separation_after_ms=60_000.0)
    patient.observe(scene(t_ms=0.0, person_at=BESIDE))
    assert patient.observe(scene(t_ms=30_000.0, person_at=None)) == ()
    assert only(patient.observe(scene(t_ms=90_000.0, person_at=None))).signal > 0.5
