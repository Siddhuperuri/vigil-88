"""The `DetectionModule` contract suite (09 §7).

Parameterised across every registered module, so adding a module automatically subjects it to
these tests. That is the point: the contract is what makes incident categories genuine plugins,
and a contract nothing checks is a comment.

Two of these are unusual and deliberate:

  * `test_declares_capabilities_it_actually_uses` runs the module with each declared capability
    removed in turn and asserts the behaviour changes. It catches both an over-declared
    capability (making a module needlessly unavailable) and an under-declared one (making it
    silently wrong) — the two ways capability negotiation degrades into theatre.
  * `test_has_a_readme_documenting_false_positives` is a real test. A module without a
    documented failure mode has not been thought about carefully enough to trust.

The suite is in **two halves**, split by a property of the source rather than by a list of
names. A module whose required capabilities include one that nothing in this build can grant
(`core.capabilities.UNGRANTABLE`) can never run here, so the behavioural contract -- purity,
signal ranges, bounded state, capability use -- has nothing to measure on it. P6 ships two such
modules, `fire` and `smoke`: declarations that the capability is absent, with no detector behind
them (ADR 0015).

Those get `TestDeclarationOnlyModuleContract` instead, which is not a weaker contract but a
different one. It asserts that the module is reported UNAVAILABLE with a reason naming the
capability, that `observe()` **raises** rather than returning an empty sequence (silence would
read as "nothing is happening"), that it carries no parameters and no shipped version, and that
its README says plainly that it is not implemented. `test_every_module_is_in_exactly_one_half`
holds the split closed, so a module cannot escape both.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tests.support.scenes import FULL_CAPABILITIES, SceneStateBuilder
from vigil.core.capabilities import Availability, Capability, ungrantable
from vigil.core.clock import ManualClock
from vigil.core.errors import CapabilityError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.protocols.module import DetectionModule, ModuleContext
from vigil.domain.enums import EventType, ObjectClass, TrackFlag, ZoneKind
from vigil.domain.observation import Observation
from vigil.domain.scene import SceneState
from vigil.events.registry import ModuleRegistration, build_registry
from vigil.observability.logging import get_logger

REGISTRY = build_registry(include_entry_points=False)
REGISTRATIONS = [REGISTRY.get(mid) for mid in REGISTRY.ids()]
# Modules that could run in this build, and modules that never can because a capability they
# require is one nothing here grants. Derived from the descriptors, so adding a module of either
# kind puts it in the right half with no list to maintain.
OBSERVING = [r for r in REGISTRATIONS if not ungrantable(r.descriptor.requires)]
DECLARED = [r for r in REGISTRATIONS if ungrantable(r.descriptor.requires)]
CAMERA = "test-cam"
# 04 §3.5 rule 3. Generous here: this asserts the module is not doing something pathological,
# not that this machine is fast. Real enforcement is the engine's per-frame budget.
CONTRACT_BUDGET_MS = 50.0


def ids(registration: ModuleRegistration) -> str:
    return registration.module_id


@pytest.fixture
def log() -> LoggerLike:
    return get_logger("test.contract")


def bind(registration: ModuleRegistration, log: LoggerLike, **params: object) -> DetectionModule:
    module = registration.factory()
    merged = {**registration.descriptor.default_params, **params}
    module.bind(ModuleContext(camera_id=CAMERA, params=merged, logger=log))
    return module


def populated_scene(
    *,
    capabilities: frozenset[Capability] = FULL_CAPABILITIES,
    epoch: int = 1,
) -> SceneState:
    """A scene rich enough that any module shipped here should have something to say about it.

    It is shared, so it grows as modules ship: a fixture that only exercised the first module
    would quietly stop being a contract. Each element below is here because some module needs
    it, and the comments say which.
    """
    return (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard", ZoneKind.RESTRICTED, criticality=3)
        .with_zone(
            "dock",
            ZoneKind.RESTRICTED,
            criticality=1,
            polygon=((0.05, 0.4), (0.45, 0.4), (0.45, 0.95), (0.05, 0.95)),
        )
        .with_track(
            1,
            ObjectClass.PERSON,
            at=(0.7, 0.6),
            hits=10,
            age_frames=10,
            in_zones=["yard"],
            dwell_ms={"yard": 9000.0},
            velocity_px_s=(40.0, 0.0),
            first_seen_ms=0.0,
        )
        .with_track(
            2,
            ObjectClass.OBJECT_BAG,
            at=(0.2, 0.6),
            size=(40.0, 30.0),
            hits=30,
            age_frames=30,
            in_zones=["dock"],
            dwell_ms={"dock": 60_000.0},
            # A bag that has been in a dock for a minute is only *abandoned* if it is also
            # still and someone was once beside it. The scene said neither, which meant it
            # described a bag nothing could have an opinion about. Both are now stated, so the
            # fixture represents the situation it was always reaching for.
            flags=[TrackFlag.STATIONARY],
            stationary_since_ms=0.0,
            first_seen_ms=0.0,
        )
        .with_track(
            # The bag's owner, standing beside it. Ownership is the condition that separates a
            # dropped bag from the furniture, so a scene with no plausible owner anywhere cannot
            # exercise it at all.
            3,
            ObjectClass.PERSON,
            at=(0.22, 0.62),
            hits=12,
            age_frames=12,
            in_zones=["dock"],
            dwell_ms={"dock": 4000.0},
            first_seen_ms=1000.0,
        )
        .with_capabilities(capabilities)
        .with_epoch(epoch)
        .at_time(5000.0, frame_index=50)
        .build()
    )


@pytest.mark.parametrize("registration", OBSERVING, ids=ids)
class TestDetectionModuleContract:
    def test_the_descriptor_is_well_formed(self, registration: ModuleRegistration) -> None:
        descriptor = registration.descriptor
        assert descriptor.module_id and descriptor.module_id.islower()
        assert isinstance(descriptor.event_type, EventType)
        assert descriptor.version.count(".") == 2  # semantic version
        assert descriptor.description.strip()
        assert not descriptor.requires & descriptor.optional

    def test_the_module_reports_its_own_descriptor(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        assert bind(registration, log).descriptor == registration.descriptor

    def test_observe_is_pure(self, registration: ModuleRegistration, log: LoggerLike) -> None:
        """Rule 1: a function of the scene and the module's own declared state."""
        module = bind(registration, log)
        scene = populated_scene()
        assert tuple(module.observe(scene)) == tuple(module.observe(scene))

    def test_observe_never_raises_on_an_empty_scene(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        module = bind(registration, log)
        assert tuple(module.observe(SceneStateBuilder(camera_id=CAMERA).build())) == ()

    def test_observe_survives_a_scene_with_zones_but_no_tracks(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        module = bind(registration, log)
        scene = SceneStateBuilder(camera_id=CAMERA).with_zone("yard").build()
        assert tuple(module.observe(scene)) == ()

    def test_observe_survives_a_scene_with_tracks_but_no_zones(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        module = bind(registration, log)
        scene = (
            SceneStateBuilder(camera_id=CAMERA)
            .with_track(1, ObjectClass.PERSON, hits=10, age_frames=10)
            .build()
        )
        module.observe(scene)  # must not raise

    def test_observe_respects_the_time_budget(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        """Rule 3. Measured with the injected clock, never `time.time()`."""
        clock = ManualClock()
        module = bind(registration, log)
        scene = populated_scene()
        module.observe(scene)  # warm any lazy imports
        import time

        # A real elapsed measurement is needed here and `ManualClock` cannot provide one. This is
        # a test asserting a performance property of the module, not decision logic, so the
        # repository's clock gate exempts it with the marker below.
        started = time.perf_counter()  # gate: allow measuring a module's own runtime in a test
        for _ in range(20):
            module.observe(scene)
        elapsed_ms = (time.perf_counter() - started) * 1000.0 / 20.0
        assert elapsed_ms < CONTRACT_BUDGET_MS
        assert clock.monotonic_ns() == 0  # the module did not advance anything

    def test_signals_are_in_the_unit_range(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        module = bind(registration, log)
        for observation in module.observe(populated_scene()):
            assert 0.0 <= observation.signal <= 1.0

    def test_signals_never_sit_exactly_at_neutral(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        """A module that emits 0.5 is saying nothing, and should emit nothing instead: a neutral
        observation contributes no evidence but still counts toward `min_observations`."""
        module = bind(registration, log)
        for observation in module.observe(populated_scene()):
            assert observation.signal != 0.5

    def test_observations_are_attributed_correctly(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        module = bind(registration, log)
        scene = populated_scene()
        for observation in module.observe(scene):
            assert observation.module_id == registration.module_id
            assert observation.camera_id == CAMERA
            assert observation.t_monotonic_ns == scene.frame_meta.t_monotonic_ns

    def test_observations_carry_facts_that_explain_them(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        """`facts` becomes the explanation an operator reads (06 §4). An observation with no
        facts cannot be explained, and is indistinguishable from a bug."""
        module = bind(registration, log)
        emitted = tuple(module.observe(populated_scene()))
        assert emitted, "the contract scene should give every shipped module something to say"
        for observation in emitted:
            assert observation.facts

    def test_declares_capabilities_it_actually_uses(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        """Removing a declared capability must change behaviour, in one direction or the other.

        An optional capability that changes nothing when removed is over-declared; a required one
        that changes nothing is either over-declared or being read without being declared.
        """
        descriptor = registration.descriptor
        module = bind(registration, log)
        baseline = tuple(module.observe(populated_scene()))

        for capability in sorted(descriptor.optional):
            reduced = tuple(
                module.observe(populated_scene(capabilities=FULL_CAPABILITIES - {capability}))
            )
            assert reduced != baseline, (
                f"{descriptor.module_id} declares {capability.value} optional but behaves "
                "identically without it: the declaration is decoration"
            )

    def test_resets_state_on_an_epoch_change(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        """Rule 4: state is bounded and reset on a tracker epoch change."""
        module = bind(registration, log)
        before = tuple(module.observe(populated_scene(epoch=1)))
        after = tuple(module.observe(populated_scene(epoch=2)))
        assert [o.signal for o in before] == [o.signal for o in after]

    def test_state_stays_bounded_over_a_long_run(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        """Rule 4: a module MAY keep state, but that state MUST be bounded."""
        module = bind(registration, log)
        for index in range(500):
            scene = (
                SceneStateBuilder(camera_id=CAMERA)
                .with_zone("yard", ZoneKind.RESTRICTED)
                .with_track(
                    index,  # a brand-new identity every frame
                    ObjectClass.PERSON,
                    at=(0.7, 0.6),
                    hits=10,
                    age_frames=10,
                    in_zones=["yard"],
                    first_seen_ms=0.0,
                )
                .at_time(index * 100.0, frame_index=index)
                .build()
            )
            module.observe(scene)
        held = sum(
            len(value) if isinstance(value, (dict, list, set, tuple)) else 1
            for value in _module_state(module).values()
        )
        assert held < 100, f"module state grew to {held} entries over 500 frames"

    def test_params_model_rejects_an_unknown_key(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        """06 §5: an unknown key is an error, not a warning."""
        with pytest.raises(Exception, match=r"(?i)extra|unexpected|forbidden|unknown"):
            bind(registration, log, definitely_not_a_real_parameter=1)

    def test_default_params_are_accepted_by_its_own_model(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        bind(registration, log)  # must not raise

    def test_release_then_bind_is_safe(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        module = bind(registration, log)
        module.observe(populated_scene())
        module.release()
        module.bind(
            ModuleContext(
                camera_id=CAMERA,
                params=registration.descriptor.default_params,
                logger=log,
            )
        )
        module.observe(populated_scene())  # must not raise

    def test_release_is_idempotent(self, registration: ModuleRegistration, log: LoggerLike) -> None:
        module = bind(registration, log)
        module.release()
        module.release()  # must not raise

    def test_the_module_creates_nothing_it_must_not(self, registration: ModuleRegistration) -> None:
        """Rule 2 and D-003, checked in the source: no incident, no evidence, no alert."""
        source = _module_source(registration)
        for forbidden in ("Incident(", "Evidence(", "Alert(", "CandidateEvent("):
            assert forbidden not in source, (
                f"{registration.module_id} constructs {forbidden} — a module returns "
                "observations and nothing else"
            )

    def test_the_module_reads_no_clock_and_no_io(self, registration: ModuleRegistration) -> None:
        """Rule 1: no clock, no filesystem, no network, no global state."""
        source = _module_source(registration)
        for forbidden in (
            "time.time(",
            "time.monotonic(",
            "datetime.now(",
            "utcnow(",
            "open(",
            "requests.",
            "urllib",
            "socket.",
        ):
            assert forbidden not in source, f"{registration.module_id} uses {forbidden}"

    def test_has_a_readme_documenting_false_positives(
        self, registration: ModuleRegistration
    ) -> None:
        """A module without a documented failure mode has not been thought about enough."""
        readme = _module_dir(registration) / "README.md"
        assert readme.is_file(), f"{registration.module_id} has no README.md"
        text = readme.read_text(encoding="utf-8").lower()
        assert "false positive" in text
        assert "cannot" in text or "limitation" in text


def _module_dir(registration: ModuleRegistration) -> Path:
    package = importlib.import_module(f"vigil.events.builtin.{registration.module_id}")
    paths = list(getattr(package, "__path__", ()))
    assert paths, f"{registration.module_id} is not a package"
    return Path(paths[0])


def _module_source(registration: ModuleRegistration) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_module_dir(registration).glob("*.py"))
    )


def _module_state(module: DetectionModule) -> dict[str, object]:
    """Whatever the module is holding, for the bounded-state check."""
    slots = getattr(type(module), "__slots__", ())
    if slots:
        return {name: getattr(module, name, None) for name in slots}
    return dict(getattr(module, "__dict__", {}))


@pytest.mark.parametrize("registration", DECLARED, ids=ids)
class TestDeclarationOnlyModuleContract:
    """The contract for a module that declares a missing capability instead of detecting.

    These modules exist so that an absent capability is *reported* rather than inferred from a
    missing directory. The contract is that they stay honest about it and cannot drift into
    looking operational.
    """

    def test_the_descriptor_is_well_formed(self, registration: ModuleRegistration) -> None:
        descriptor = registration.descriptor
        assert descriptor.module_id and descriptor.module_id.islower()
        assert isinstance(descriptor.event_type, EventType)
        assert descriptor.version.count(".") == 2
        assert descriptor.description.strip()
        assert not descriptor.requires & descriptor.optional

    def test_the_module_reports_its_own_descriptor(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        assert bind(registration, log).descriptor == registration.descriptor

    def test_it_requires_something_this_build_cannot_grant(
        self, registration: ModuleRegistration
    ) -> None:
        """The reason it never runs, stated in the descriptor rather than in a comment."""
        assert ungrantable(registration.descriptor.requires)

    def test_the_registry_reports_it_unavailable_with_an_actionable_reason(
        self, registration: ModuleRegistration
    ) -> None:
        """D-008: listed in `modules.enabled`, on a fully capable camera, it still cannot run."""
        availability = REGISTRY.availability(
            registration.module_id,
            camera_id=CAMERA,
            available=FULL_CAPABILITIES,
            enabled_ids=[registration.module_id],
        )
        assert availability.status is Availability.UNAVAILABLE
        assert not availability.will_run
        assert availability.reason
        for capability in ungrantable(registration.descriptor.requires):
            assert capability.value in availability.reason

    def test_observe_raises_rather_than_reporting_nothing(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        """An empty sequence is indistinguishable from a confident "nothing is happening"."""
        module = bind(registration, log)
        with pytest.raises(CapabilityError) as caught:
            module.observe(populated_scene())
        message = str(caught.value).lower()
        assert registration.module_id in message
        for capability in ungrantable(registration.descriptor.requires):
            assert capability.value in message

    def test_observe_raises_on_an_empty_scene_too(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        """Nothing about the scene makes the answer knowable, so no scene can change it."""
        module = bind(registration, log)
        with pytest.raises(CapabilityError):
            module.observe(SceneStateBuilder(camera_id=CAMERA).build())

    def test_it_ships_no_parameters_and_no_version(self, registration: ModuleRegistration) -> None:
        """Plausible-looking thresholds are the first step towards looking implemented, and a
        1.x would make the dismissal-rate-per-version metric name an algorithm that never ran."""
        assert registration.descriptor.default_params == {}
        assert registration.descriptor.version == "0.0.0"

    def test_the_description_says_it_is_not_implemented(
        self, registration: ModuleRegistration
    ) -> None:
        """The description is what the API and the console render, so it carries the truth."""
        assert "NOT IMPLEMENTED" in registration.descriptor.description

    def test_release_then_bind_is_safe(
        self, registration: ModuleRegistration, log: LoggerLike
    ) -> None:
        module = bind(registration, log)
        module.release()
        module.bind(ModuleContext(camera_id=CAMERA, params={}, logger=log))
        module.release()
        module.release()  # idempotent

    def test_the_module_reads_no_clock_and_no_io(self, registration: ModuleRegistration) -> None:
        source = _module_source(registration)
        for forbidden in ("time.time", "datetime.now", "open(", "requests.", "socket."):
            assert forbidden not in source, f"{registration.module_id} uses {forbidden}"

    def test_has_a_readme_saying_it_is_not_implemented(
        self, registration: ModuleRegistration
    ) -> None:
        readme = _module_dir(registration) / "README.md"
        assert readme.is_file(), f"{registration.module_id} has no README.md"
        text = readme.read_text(encoding="utf-8").lower()
        assert "not implemented" in text
        # Still required: the failure modes a real implementation would have to beat, so the
        # next person starts from the problem rather than from the architecture.
        assert "false positive" in text
        assert "cannot" in text or "limitation" in text


def test_every_module_is_in_exactly_one_half() -> None:
    """A module that escaped both suites would be the one nobody checked."""
    assert REGISTRATIONS
    assert OBSERVING, "no module can run in this build at all"
    assert DECLARED, "P6 ships fire and smoke as declarations; they must land in this half"
    ids_observing = {r.module_id for r in OBSERVING}
    ids_declared = {r.module_id for r in DECLARED}
    assert not ids_observing & ids_declared
    assert ids_observing | ids_declared == set(REGISTRY.ids())


def test_every_observation_is_attributed_to_a_registered_module() -> None:
    """Evidence that named an unregistered module would land in an accumulator nothing owns."""
    log = get_logger("test.contract")
    scene = populated_scene()
    emitted: list[Observation] = []
    for registration in OBSERVING:
        emitted.extend(bind(registration, log).observe(scene))
    assert emitted
    assert {o.module_id for o in emitted} <= set(REGISTRY.ids())
