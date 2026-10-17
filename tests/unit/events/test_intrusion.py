"""The intrusion module as a pure function of the scene (06 §4, §14).

No pipeline, no detector, no frames, no clock — a hand-built `SceneState` and one call. The
temporal behaviour of these observations is tested separately; here the question is only whether
the module reads the scene correctly and scores it as specified.
"""

from __future__ import annotations

import pytest

from tests.support.scenes import SceneStateBuilder
from vigil.core.capabilities import Capability
from vigil.core.protocols.logger import LoggerLike
from vigil.core.protocols.module import ModuleContext
from vigil.domain.enums import ObjectClass, SceneFact, TrackFlag, ZoneKind
from vigil.domain.observation import Observation
from vigil.domain.scene import Schedule
from vigil.events.builtin.intrusion import MODULE, IntrusionModule, IntrusionParams, create
from vigil.observability.logging import get_logger

CAMERA = "test-cam"


@pytest.fixture
def log() -> LoggerLike:
    return get_logger("test.intrusion")


def bound(log: LoggerLike, **params: object) -> IntrusionModule:
    module = create()
    merged = {**MODULE.default_params, **params}
    module.bind(ModuleContext(camera_id=CAMERA, params=merged, logger=log))
    return module


def inside_zone(**track: object) -> SceneStateBuilder:
    """One person standing well inside one active restricted zone."""
    builder = SceneStateBuilder(camera_id=CAMERA).with_zone(
        "yard", ZoneKind.RESTRICTED, criticality=3
    )
    defaults: dict[str, object] = {
        "object_class": ObjectClass.PERSON,
        "at": (0.7, 0.6),
        "hits": 5,
        "in_zones": ["yard"],
    }
    return builder.with_track(1, **{**defaults, **track})  # type: ignore[arg-type]


def only(observations: object) -> Observation:
    assert isinstance(observations, tuple)
    assert len(observations) == 1, f"expected exactly one observation, got {len(observations)}"
    result = observations[0]
    assert isinstance(result, Observation)
    return result


# ------------------------------------------------------------------ descriptor and contract


def test_the_descriptor_declares_what_the_module_actually_uses() -> None:
    assert MODULE.module_id == "intrusion"
    assert MODULE.event_type.value == "intrusion"
    assert MODULE.requires == frozenset(
        {Capability.DETECTION, Capability.TRACKING, Capability.ZONES}
    )
    assert MODULE.optional == frozenset({Capability.VELOCITY})


def test_default_params_validate_against_the_module_s_own_model() -> None:
    params = IntrusionParams.model_validate(dict(MODULE.default_params))
    assert params.base_signal == pytest.approx(0.70)
    assert params.object_classes == (ObjectClass.PERSON,)


def test_an_unknown_parameter_is_an_error_at_bind_time(log: LoggerLike) -> None:
    """A silently-ignored typo in a threshold is indistinguishable from the module not working."""
    module = create()
    with pytest.raises(ValueError, match=r"base_signl|extra_forbidden|Extra inputs"):
        module.bind(
            ModuleContext(
                camera_id=CAMERA,
                params={**MODULE.default_params, "base_signl": 0.9},
                logger=log,
            )
        )


def test_observing_before_bind_is_an_explicit_error() -> None:
    with pytest.raises(RuntimeError, match="before bind"):
        create().observe(inside_zone().build())


def test_an_exclusion_zone_cannot_be_policed_as_restricted(log: LoggerLike) -> None:
    with pytest.raises(ValueError, match="invert the meaning"):
        bound(log, zone_kinds=["exclusion"])


def test_signals_that_could_reach_certainty_from_one_frame_are_refused(log: LoggerLike) -> None:
    with pytest.raises(ValueError, match="defeats temporal accumulation"):
        bound(log, base_signal=0.9, velocity_bonus_ratio=0.5, dwell_bonus_ratio=0.5)


# ------------------------------------------------------------------ the basic case


def test_a_tracked_person_inside_an_active_restricted_zone_is_observed(log: LoggerLike) -> None:
    observation = only(bound(log).observe(inside_zone().build()))
    assert observation.camera_id == CAMERA
    assert observation.module_id == "intrusion"
    assert str(observation.subject) == "track:1"
    assert observation.signal == pytest.approx(0.70, abs=0.01)


def test_the_observation_is_stamped_with_the_frame_s_time_not_a_clock(log: LoggerLike) -> None:
    builder = inside_zone().at_time(4321.0)
    observation = only(bound(log).observe(builder.build()))
    assert observation.t_monotonic_ns == builder.frame_meta().t_monotonic_ns


def test_the_facts_explain_the_signal(log: LoggerLike) -> None:
    """`facts` is not decoration: it is the explanation an operator reads (06 §4)."""
    observation = only(bound(log).observe(inside_zone().build()))
    assert observation.facts["zone_id"] == "yard"
    assert observation.facts["zone_kind"] == "restricted"
    assert observation.facts["zone_criticality"] == pytest.approx(3.0)
    assert observation.facts["object_class"] == "person"
    assert observation.facts["hits"] == pytest.approx(5.0)
    assert observation.facts["base_signal"] == pytest.approx(0.70)
    assert observation.facts["signal"] == pytest.approx(observation.signal, abs=1e-6)


def test_observe_is_pure(log: LoggerLike) -> None:
    module = bound(log)
    scene = inside_zone().build()
    assert module.observe(scene) == module.observe(scene)


def test_an_empty_scene_produces_nothing_and_does_not_raise(log: LoggerLike) -> None:
    assert bound(log).observe(SceneStateBuilder(camera_id=CAMERA).build()) == ()


def test_a_zone_with_no_one_in_it_produces_nothing(log: LoggerLike) -> None:
    scene = SceneStateBuilder(camera_id=CAMERA).with_zone("yard").build()
    assert bound(log).observe(scene) == ()


# ------------------------------------------------------------------ gating on the scene


def test_a_person_outside_every_zone_is_not_observed(log: LoggerLike) -> None:
    scene = inside_zone(at=(0.1, 0.6), in_zones=[]).build()
    assert bound(log).observe(scene) == ()


def test_a_zone_of_an_unpoliced_kind_is_ignored(log: LoggerLike) -> None:
    scene = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("atrium", ZoneKind.COUNTING)
        .with_track(1, ObjectClass.PERSON, in_zones=["atrium"], hits=5)
        .build()
    )
    assert bound(log).observe(scene) == ()


def test_a_zone_outside_its_schedule_is_ignored(log: LoggerLike) -> None:
    """A restricted yard active 18:00-06:00 must not fire at 14:00."""
    overnight = Schedule(start_minute=18 * 60, end_minute=6 * 60)
    builder = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard", ZoneKind.RESTRICTED, schedule=overnight)
        .with_track(1, ObjectClass.PERSON, in_zones=["yard"], hits=5)
    )
    builder.wall_epoch = builder.wall_epoch.replace(hour=14)
    assert bound(log).observe(builder.build()) == ()


def test_a_zone_inside_its_schedule_is_policed(log: LoggerLike) -> None:
    overnight = Schedule(start_minute=18 * 60, end_minute=6 * 60)
    builder = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard", ZoneKind.RESTRICTED, schedule=overnight)
        .with_track(1, ObjectClass.PERSON, in_zones=["yard"], hits=5)
    )
    builder.wall_epoch = builder.wall_epoch.replace(hour=2, minute=41)
    assert len(bound(log).observe(builder.build())) == 1


@pytest.mark.parametrize(
    "object_class", [ObjectClass.VEHICLE_CAR, ObjectClass.ANIMAL, ObjectClass.UNKNOWN]
)
def test_a_class_the_module_does_not_police_is_ignored(
    log: LoggerLike, object_class: ObjectClass
) -> None:
    scene = inside_zone(object_class=object_class).build()
    assert bound(log).observe(scene) == ()


def test_an_extra_configured_class_is_policed(log: LoggerLike) -> None:
    scene = inside_zone(object_class=ObjectClass.VEHICLE_CAR).build()
    module = bound(log, object_classes=["person", "vehicle.car"])
    assert len(module.observe(scene)) == 1


def test_a_track_with_too_few_hits_is_ignored(log: LoggerLike) -> None:
    """The worked example's first frame: hits=1, so the module says nothing (06 §12)."""
    assert bound(log).observe(inside_zone(hits=1).build()) == ()
    assert bound(log).observe(inside_zone(hits=2).build()) == ()
    assert len(bound(log).observe(inside_zone(hits=3).build())) == 1


def test_a_mostly_predicted_track_is_ignored(log: LoggerLike) -> None:
    """A track 60 frames old with 6 hits is hallucinated motion, not evidence."""
    scene = inside_zone(hits=6, age_frames=60).build()
    assert bound(log).observe(scene) == ()


def test_an_unconfirmed_track_is_ignored(log: LoggerLike) -> None:
    scene = inside_zone(flags=[TrackFlag.NEW]).build()
    assert bound(log).observe(scene) == ()


def test_degraded_occupancy_produces_silence_rather_than_a_guess(log: LoggerLike) -> None:
    scene = inside_zone().with_degraded_fact(SceneFact.ZONE_OCCUPANCY).build()
    assert bound(log).observe(scene) == ()


def test_occupancy_naming_a_track_the_scene_does_not_carry_is_survived(
    log: LoggerLike,
) -> None:
    """A malformed scene must not crash the module: an object we cannot describe cannot be
    reported on, so it is skipped."""
    builder = inside_zone()
    scene = builder.build()
    broken = type(scene)(
        frame_meta=scene.frame_meta,
        tracks=(),  # occupancy still names track 1
        zones=scene.zones,
        occupancy=scene.occupancy,
        relations=scene.relations,
        stability_ratio=scene.stability_ratio,
        capabilities=scene.capabilities,
        degraded_facts=scene.degraded_facts,
    )
    assert bound(log).observe(broken) == ()


# ------------------------------------------------------------------ scoring terms


def test_dwelling_raises_the_signal_once_the_threshold_is_passed(log: LoggerLike) -> None:
    module = bound(log)
    brief = only(module.observe(inside_zone(dwell_ms={"yard": 500.0}).build()))
    settled = only(module.observe(inside_zone(dwell_ms={"yard": 9000.0}).build()))
    assert settled.signal > brief.signal
    assert settled.facts["dwell_bonus"] == pytest.approx(0.10)
    assert "dwell_bonus" not in brief.facts


def test_standing_near_the_boundary_lowers_the_signal(log: LoggerLike) -> None:
    """Boundary flicker is this module's main false positive, so the edge is scored down."""
    module = bound(log)
    deep = only(module.observe(inside_zone(at=(0.7, 0.6)).build()))
    edge = only(module.observe(inside_zone(at=(0.505, 0.6)).build()))
    assert edge.signal < deep.signal
    assert edge.facts["edge_penalty"] == pytest.approx(0.15)
    assert "edge_penalty" not in deep.facts


def test_the_signal_never_falls_to_or_below_neutral(log: LoggerLike) -> None:
    """A penalised but real presence is weak evidence *for* an intrusion, never against it:
    a signal at or below 0.5 would subtract evidence and could never confirm."""
    module = bound(log, base_signal=0.55, edge_penalty_ratio=0.5)
    observation = only(module.observe(inside_zone(at=(0.5005, 0.6)).build()))
    assert observation.signal > 0.5


def test_moving_deeper_into_the_zone_raises_the_signal(log: LoggerLike) -> None:
    module = bound(log)
    inward = only(module.observe(inside_zone(at=(0.6, 0.6), velocity_px_s=(60.0, 0.0)).build()))
    still = only(module.observe(inside_zone(at=(0.6, 0.6)).build()))
    assert inward.signal > still.signal
    assert inward.facts["velocity_bonus"] > 0.0


def test_moving_out_of_the_zone_earns_no_inward_bonus(log: LoggerLike) -> None:
    module = bound(log)
    outward = only(module.observe(inside_zone(at=(0.52, 0.6), velocity_px_s=(-200.0, 0.0)).build()))
    assert outward.facts.get("velocity_bonus", 0.0) == pytest.approx(0.0)


def test_a_stationary_track_earns_no_direction_bonus(log: LoggerLike) -> None:
    scene = inside_zone(
        at=(0.7, 0.6),
        velocity_px_s=(0.5, 0.0),
        flags=[TrackFlag.STATIONARY],
        stationary_since_ms=0.0,
    ).build()
    observation = only(bound(log).observe(scene))
    assert "velocity_bonus" not in observation.facts


def test_without_the_velocity_capability_the_bonus_is_simply_not_applied(
    log: LoggerLike,
) -> None:
    """DEGRADED means the module still works, with one term missing — and says so."""
    scene = (
        inside_zone(at=(0.6, 0.6), velocity_px_s=(60.0, 0.0))
        .without_capability(Capability.VELOCITY)
        .build()
    )
    observation = only(bound(log).observe(scene))
    assert observation.facts["velocity_unavailable"] is True
    assert "velocity_bonus" not in observation.facts
    assert observation.signal == pytest.approx(0.70, abs=0.01)


def test_the_signal_stays_strictly_inside_the_unit_interval(log: LoggerLike) -> None:
    module = bound(log, base_signal=0.8, velocity_bonus_ratio=0.1, dwell_bonus_ratio=0.09)
    scene = inside_zone(
        at=(0.7, 0.6), velocity_px_s=(100.0, 0.0), dwell_ms={"yard": 60_000.0}
    ).build()
    observation = only(module.observe(scene))
    assert 0.0 < observation.signal < 1.0


# ------------------------------------------------------------------ multiplicity (§14)


def test_each_person_in_a_zone_is_evaluated_independently(log: LoggerLike) -> None:
    """Two people in one zone are two hypotheses, not one twice as strong (06 §3)."""
    scene = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard", ZoneKind.RESTRICTED)
        .with_track(12, ObjectClass.PERSON, at=(0.7, 0.6), hits=5, in_zones=["yard"])
        .with_track(19, ObjectClass.PERSON, at=(0.8, 0.7), hits=5, in_zones=["yard"])
        .build()
    )
    observations = bound(log).observe(scene)
    assert {str(o.subject) for o in observations} == {"track:12", "track:19"}


def test_one_person_in_two_zones_is_observed_once_per_zone(log: LoggerLike) -> None:
    scene = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard", ZoneKind.RESTRICTED)
        .with_zone(
            "dock", ZoneKind.RESTRICTED, polygon=((0.4, 0.3), (0.9, 0.3), (0.9, 0.9), (0.4, 0.9))
        )
        .with_track(1, ObjectClass.PERSON, at=(0.7, 0.6), hits=5, in_zones=["yard", "dock"])
        .build()
    )
    observations = bound(log).observe(scene)
    assert {o.facts["zone_id"] for o in observations} == {"yard", "dock"}
    assert {str(o.subject) for o in observations} == {"track:1"}


def test_observations_are_emitted_in_a_deterministic_order(log: LoggerLike) -> None:
    module = bound(log)
    scene = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard", ZoneKind.RESTRICTED)
        .with_track(31, ObjectClass.PERSON, at=(0.7, 0.6), hits=5, in_zones=["yard"])
        .with_track(7, ObjectClass.PERSON, at=(0.8, 0.7), hits=5, in_zones=["yard"])
        .with_track(14, ObjectClass.PERSON, at=(0.9, 0.8), hits=5, in_zones=["yard"])
        .build()
    )
    assert [str(o.subject) for o in module.observe(scene)] == [
        "track:7",
        "track:14",
        "track:31",
    ]


# ------------------------------------------------------------------ state and epochs (rule 4)


def test_the_module_keeps_no_state_that_changes_its_answer(log: LoggerLike) -> None:
    """Rule 1: observe() is a function of the scene. Running a hundred frames first must not
    change what the hundred-and-first returns."""
    module = bound(log)
    scene = inside_zone().build()
    first = module.observe(scene)
    for i in range(100):
        module.observe(inside_zone().at_time(float(i) * 100.0).build())
    assert module.observe(scene) == first


def test_an_epoch_change_is_absorbed_without_affecting_the_answer(log: LoggerLike) -> None:
    module = bound(log)
    before = module.observe(inside_zone().build())
    after = module.observe(inside_zone().with_epoch(2).build())
    assert [o.signal for o in before] == [o.signal for o in after]


def test_release_then_bind_restores_a_usable_module(log: LoggerLike) -> None:
    module = bound(log)
    module.release()
    module.bind(ModuleContext(camera_id=CAMERA, params=MODULE.default_params, logger=log))
    assert len(module.observe(inside_zone().build())) == 1


# ------------------------------------------------------------------ parameter validation


def test_an_empty_object_class_list_is_rejected(log: LoggerLike) -> None:
    with pytest.raises(ValueError, match="object_classes must not be empty"):
        bound(log, object_classes=[])


def test_an_empty_zone_kind_list_is_rejected(log: LoggerLike) -> None:
    with pytest.raises(ValueError, match="zone_kinds must not be empty"):
        bound(log, zone_kinds=[])


def test_a_degenerate_zone_polygon_is_survived(log: LoggerLike) -> None:
    """Defensive: the scene analyzer validates polygons, but a module handed a bad one must
    not crash the pipeline for every other module on the camera."""
    from vigil.core.geometry import Point, Polygon
    from vigil.domain.scene import Zone

    builder = inside_zone()
    scene = builder.build()
    flat = Zone(
        zone_id="yard",
        camera_id=CAMERA,
        kind=ZoneKind.RESTRICTED,
        polygon=Polygon([Point(0.5, 0.5), Point(0.6, 0.5), Point(0.7, 0.5)]),
        criticality=1,
        active_schedule=None,
        label="flat",
    )
    degenerate = type(scene)(
        frame_meta=scene.frame_meta,
        tracks=scene.tracks,
        zones=(flat,),
        occupancy=scene.occupancy,
        relations=scene.relations,
        stability_ratio=scene.stability_ratio,
        capabilities=scene.capabilities,
        degraded_facts=scene.degraded_facts,
    )
    observations = bound(log).observe(degenerate)  # must not raise
    assert len(observations) == 1
