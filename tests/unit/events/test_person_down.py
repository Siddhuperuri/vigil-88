"""person_down: what the geometry says, and what it refuses to say.

Every test here builds a `SceneState` by hand and calls `observe` — no pipeline, no detector,
no clock (09 §12). That is the contract working: if a module needed more setup than this, the
contract would be leaking.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from tests.support.scenes import FULL_CAPABILITIES, SceneStateBuilder
from vigil.core.capabilities import Capability
from vigil.core.protocols.module import ModuleContext
from vigil.domain.enums import EventType, ObjectClass, TrackFlag
from vigil.domain.observation import Observation
from vigil.events.builtin.person_down import MODULE, PersonDownModule, PersonDownParams, create
from vigil.observability.logging import get_logger

CAMERA = "test-cam"
# Default thresholds: upright aspect 0.55, prone 1.0; collapse at 0.55 of the reference height.
STANDING = (60.0, 170.0)
PRONE = (180.0, 55.0)


def module(**params: object) -> PersonDownModule:
    built = create()
    built.bind(
        ModuleContext(
            camera_id=CAMERA,
            params={**MODULE.default_params, **params},
            logger=get_logger("test.person_down"),
        )
    )
    return built


def scene(
    *,
    size: tuple[float, float] = STANDING,
    velocity: tuple[float, float] | None = (0.0, 0.0),
    hits: int = 10,
    age: int | None = None,
    flags: Sequence[TrackFlag] = (),
    capabilities: frozenset[Capability] = FULL_CAPABILITIES,
    stationary_since_ms: float | None = None,
    t_ms: float = 5000.0,
    history: Sequence[tuple[float, float]] = (),
) -> object:
    """One person, optionally with a history of earlier box sizes for the collapse reference."""
    builder = SceneStateBuilder(camera_id=CAMERA).with_capabilities(capabilities)
    builder.t_ms = t_ms
    builder.with_track(
        1,
        ObjectClass.PERSON,
        at=(0.5, 0.8),
        size=size,
        hits=hits,
        age_frames=hits if age is None else age,
        velocity_px_s=velocity,
        flags=flags,
        stationary_since_ms=stationary_since_ms,
        first_seen_ms=0.0,
    )
    built = builder.build()
    if history:
        built = _with_history(built, history, t_ms=t_ms)
    return built


def _with_history(built: object, sizes: Sequence[tuple[float, float]], *, t_ms: float) -> object:
    """Replace the track's trajectory with measured points of the given box sizes.

    The standing reference comes from the track's own history, so a test about collapse has to
    be able to say what the subject used to look like.
    """
    from dataclasses import replace

    from vigil.core.geometry import BBox
    from vigil.core.units import ms_to_ns
    from vigil.domain.track import TrackPoint, Trajectory

    track = built.tracks[0]  # type: ignore[attr-defined]
    points = []
    for index, (w, h) in enumerate(sizes):
        foot_x, foot_y = 0.5 * 1280, 0.8 * 720
        points.append(
            TrackPoint(
                t_monotonic_ns=ms_to_ns(index * 100.0),
                bbox=BBox(foot_x - w / 2, foot_y - h, foot_x + w / 2, foot_y),
                confidence_ratio=0.9,
                is_interpolated=False,
            )
        )
    points.append(
        TrackPoint(
            t_monotonic_ns=ms_to_ns(t_ms),
            bbox=track.bbox,
            confidence_ratio=0.9,
            is_interpolated=False,
        )
    )
    enriched = replace(track, trajectory=Trajectory(tuple(points), 90))
    return replace(built, tracks=(enriched,))  # type: ignore[arg-type]


def only(emitted: Sequence[Observation]) -> Observation:
    assert len(emitted) == 1, f"expected one observation, got {len(emitted)}"
    return emitted[0]


def signal_of(built: object, **params: object) -> float:
    return only(module(**params).observe(built)).signal  # type: ignore[arg-type]


# ---------------------------------------------------------------- the descriptor


def test_the_descriptor_declares_no_pose_capability() -> None:
    """Declaring POSE as optional-but-unused would report a degradation that changes nothing."""
    assert Capability.POSE not in MODULE.requires
    assert Capability.POSE not in MODULE.optional
    assert MODULE.requires == {Capability.DETECTION, Capability.TRACKING}
    assert MODULE.optional == {Capability.VELOCITY}
    assert MODULE.event_type is EventType.PERSON_DOWN


def test_the_description_refuses_to_claim_injury() -> None:
    """The one failure that would matter most is an operator reading this as a medical alarm."""
    text = MODULE.description.lower()
    assert "no pose model" in text or "geometric only" in text
    assert "no claim about injury" in text or "makes no claim" in text
    for word in ("injured", "unconscious", "medical emergency", "casualty"):
        assert word not in text


# ---------------------------------------------------------------- the terms


def test_an_upright_walking_person_is_evidence_against() -> None:
    """Below neutral, not silence: it is what releases an event when someone stands up."""
    emitted = only(module().observe(scene(velocity=(90.0, 0.0))))
    assert emitted.signal < 0.5
    assert emitted.facts["aspect_term"] == 0.0


def test_a_prone_immobile_person_is_strong_evidence_for() -> None:
    built = scene(
        size=PRONE,
        velocity=(0.0, 0.0),
        history=[STANDING] * 10,
        flags=[TrackFlag.STATIONARY],
        stationary_since_ms=0.0,
    )
    emitted = only(module().observe(built))
    assert emitted.signal > 0.8
    assert emitted.facts["aspect_term"] == 1.0
    assert emitted.facts["collapse_term"] == 1.0
    assert emitted.facts["immobility_term"] == 1.0


def test_the_aspect_term_ramps_between_upright_and_prone() -> None:
    upright = signal_of(scene(size=(60.0, 170.0)))
    midway = signal_of(scene(size=(100.0, 130.0)))  # aspect ~0.77, between 0.55 and 1.0
    prone = signal_of(scene(size=PRONE))
    assert upright < midway < prone


def test_collapse_is_measured_against_the_track_s_own_history() -> None:
    """Relative, not absolute: it needs no knowledge of camera geometry or distance."""
    collapsed = only(module().observe(scene(size=(90.0, 70.0), history=[(60.0, 200.0)] * 10)))
    assert collapsed.facts["height_ratio"] == pytest.approx(70.0 / 200.0, abs=0.01)
    assert collapsed.facts["collapse_term"] == 1.0

    # The same box, for someone who was always that size: no collapse.
    always_small = only(module().observe(scene(size=(90.0, 70.0), history=[(90.0, 70.0)] * 10)))
    assert always_small.facts["collapse_term"] == 0.0
    assert collapsed.signal > always_small.signal


def test_a_person_first_seen_already_down_has_no_standing_reference() -> None:
    """The gap is named rather than papered over: the other terms then decide."""
    emitted = only(module().observe(scene(size=PRONE, history=[])))
    assert "collapse_term" not in emitted.facts
    assert emitted.facts["collapse_unavailable"]


def test_the_reference_ignores_interpolated_points() -> None:
    """A predicted box is the filter's extrapolation; a reference from one compares the subject
    against a guess about itself."""
    from dataclasses import replace

    from vigil.core.geometry import BBox
    from vigil.core.units import ms_to_ns
    from vigil.domain.track import TrackPoint, Trajectory

    built = scene(size=PRONE, history=[STANDING] * 6)
    track = built.tracks[0]  # type: ignore[attr-defined]
    points = list(track.trajectory.points)
    # A tall *interpolated* point, inserted in time order, that would dominate the reference if
    # predicted boxes were counted.
    tall = TrackPoint(ms_to_ns(600.0), BBox(0.0, 0.0, 50.0, 900.0), 0.9, True)
    points.insert(-1, tall)
    with_ghost = replace(track, trajectory=Trajectory(tuple(points), 90))
    emitted = only(module().observe(replace(built, tracks=(with_ghost,))))  # type: ignore[arg-type]
    assert emitted.facts["reference_height_px"] == pytest.approx(170.0, abs=1.0)


def test_a_short_history_is_not_a_reference() -> None:
    emitted = only(module().observe(scene(size=PRONE, history=[STANDING] * 2)))
    assert emitted.facts["collapse_unavailable"]


# ---------------------------------------------------------------- degraded behaviour


def test_without_velocity_the_remaining_weights_are_renormalised() -> None:
    """A dropped term must not quietly become evidence against."""
    built = scene(size=PRONE, history=[STANDING] * 10, velocity=(0.0, 0.0))
    with_velocity = only(module().observe(built))

    degraded_scene = scene(
        size=PRONE,
        history=[STANDING] * 10,
        velocity=(0.0, 0.0),
        capabilities=FULL_CAPABILITIES - {Capability.VELOCITY},
    )
    degraded = only(module().observe(degraded_scene))

    assert degraded.facts["velocity_unavailable"] is True
    assert "immobility_term" not in degraded.facts
    # Both terms that remain are saturated, so the score is still 1.0 -- renormalised, not
    # penalised for the missing one.
    assert degraded.facts["score"] == pytest.approx(1.0)
    assert degraded.signal == pytest.approx(with_velocity.signal, abs=0.06)


def test_a_moving_person_scores_lower_than_a_still_one() -> None:
    still = signal_of(scene(size=PRONE, history=[STANDING] * 10, velocity=(0.0, 0.0)))
    moving = signal_of(scene(size=PRONE, history=[STANDING] * 10, velocity=(200.0, 0.0)))
    assert moving < still


def test_the_stationary_flag_substitutes_for_a_missing_velocity_fit() -> None:
    """The tracker has already judged it still; no fitted velocity yet is not a reason to
    ignore that."""
    emitted = only(
        module().observe(
            scene(size=PRONE, history=[STANDING] * 10, velocity=None, flags=[TrackFlag.STATIONARY])
        )
    )
    assert emitted.facts["immobility_term"] == 1.0


def test_an_unknown_velocity_with_no_stationary_flag_drops_the_term() -> None:
    emitted = only(module().observe(scene(size=PRONE, history=[STANDING] * 10, velocity=None)))
    assert emitted.facts["velocity_unavailable"] is True


def test_long_stillness_adds_a_bonus_the_trajectory_cannot_express() -> None:
    """`stationary_since` outlives the bounded history, so it is the only term that can speak
    to minutes."""
    brief = signal_of(
        scene(size=PRONE, history=[STANDING] * 10, flags=[TrackFlag.STATIONARY], t_ms=5000.0)
    )
    sustained = signal_of(
        scene(
            size=PRONE,
            history=[STANDING] * 10,
            flags=[TrackFlag.STATIONARY],
            stationary_since_ms=0.0,
            t_ms=60_000.0,
        )
    )
    assert sustained >= brief


# ---------------------------------------------------------------- gating and hygiene


def test_a_new_or_low_quality_track_is_ignored() -> None:
    assert module().observe(scene(hits=2)) == ()
    assert module().observe(scene(flags=[TrackFlag.NEW], hits=1)) == ()
    assert module().observe(scene(hits=5, age=40)) == ()  # hit ratio 0.125


def test_a_non_person_class_is_ignored() -> None:
    built = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_track(1, ObjectClass.OBJECT_BAG, size=PRONE, hits=20, age_frames=20)
        .build()
    )
    assert module().observe(built) == ()


def test_the_signal_never_sits_at_neutral() -> None:
    """An ambiguous posture is an absence of support, not support: it resolves below neutral."""
    for width in range(60, 200, 4):
        for height in range(40, 200, 4):
            built = scene(size=(float(width), float(height)), history=[STANDING] * 8)
            emitted = module().observe(built)
            if emitted:
                assert emitted[0].signal != 0.5


def test_the_signal_stays_inside_the_configured_band() -> None:
    for size in [(10.0, 300.0), PRONE, (300.0, 10.0), (100.0, 100.0)]:
        built = scene(size=size, history=[STANDING] * 10, flags=[TrackFlag.STATIONARY])
        emitted = module().observe(built)
        if emitted:
            assert 0.1 <= emitted[0].signal <= 0.9


def test_every_observation_explains_itself() -> None:
    emitted = only(module().observe(scene(size=PRONE, history=[STANDING] * 10)))
    assert emitted.facts["method"] == "bounding-box geometry; no pose model"
    assert "aspect_ratio" in emitted.facts
    assert "score" in emitted.facts
    assert "signal" in emitted.facts


def test_two_people_are_two_independent_hypotheses() -> None:
    built = (
        SceneStateBuilder(camera_id=CAMERA)
        .with_track(1, ObjectClass.PERSON, at=(0.3, 0.8), size=PRONE, hits=10, age_frames=10)
        .with_track(2, ObjectClass.PERSON, at=(0.7, 0.8), size=STANDING, hits=10, age_frames=10)
        .build()
    )
    emitted = module().observe(built)
    assert {str(o.subject) for o in emitted} == {"track:1", "track:2"}
    by_subject = {str(o.subject): o.signal for o in emitted}
    assert by_subject["track:1"] > by_subject["track:2"]


def test_observing_the_same_scene_twice_says_the_same_thing() -> None:
    built = scene(size=PRONE, history=[STANDING] * 10)
    instance = module()
    assert tuple(instance.observe(built)) == tuple(instance.observe(built))


def test_the_module_holds_no_per_track_state() -> None:
    """Nothing to reset, which is why `observe` is pure in the strict sense."""
    instance = module()
    for index in range(200):
        instance.observe(scene(t_ms=index * 100.0))
    assert set(PersonDownModule.__slots__) == {"_epoch", "_params"}


# ---------------------------------------------------------------- parameters


def test_the_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError, match=r"sum to 1\.0"):
        PersonDownParams(aspect_weight=0.9, collapse_weight=0.9, immobility_weight=0.9)


def test_an_inverted_threshold_pair_is_refused() -> None:
    with pytest.raises(ValidationError, match="below prone_aspect_ratio"):
        PersonDownParams(upright_aspect_ratio=1.2, prone_aspect_ratio=1.0)
    with pytest.raises(ValidationError, match="below no_collapse_ratio"):
        PersonDownParams(collapse_ratio=0.95, no_collapse_ratio=0.9)
    with pytest.raises(ValidationError, match="below moving_speed_ratio"):
        PersonDownParams(immobile_speed_ratio=0.9, moving_speed_ratio=0.5)


def test_an_empty_object_class_list_is_refused() -> None:
    with pytest.raises(ValidationError, match="never fire"):
        PersonDownParams(object_classes=())


def test_an_unknown_parameter_is_an_error_not_a_warning() -> None:
    with pytest.raises(ValidationError):
        PersonDownParams(prone_aspekt_ratio=1.0)  # type: ignore[call-arg]


def test_tightening_the_band_tightens_the_signal() -> None:
    built = scene(size=PRONE, history=[STANDING] * 10, flags=[TrackFlag.STATIONARY])
    default = signal_of(built)
    narrow = signal_of(built, signal_ceiling=0.7)
    assert narrow < default
    assert narrow <= 0.7
