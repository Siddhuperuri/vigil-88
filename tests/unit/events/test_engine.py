"""Failure containment, budget enforcement, epoch reset and binding (04 §3.5 rules 3-5)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from tests.support.scenes import FULL_CAPABILITIES, SceneStateBuilder
from vigil.config.schema.camera import CameraConfig, SourceConfig
from vigil.config.schema.modules import ModulesConfig
from vigil.core.capabilities import Availability, Capability
from vigil.core.clock import Clock, ManualClock
from vigil.core.errors import ModuleError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.protocols.module import ModuleContext, ModuleDescriptor
from vigil.domain.enums import EventType, ObjectClass, ZoneKind
from vigil.domain.observation import Observation, SubjectKey
from vigil.domain.scene import SceneState
from vigil.events.binding import bind_modules, required_capabilities, resolve_params
from vigil.events.engine import EventEngine, ModuleRuntime
from vigil.events.registry import (
    ModuleAvailability,
    ModuleRegistry,
    build_registry,
    discover_builtin,
)
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry

CAMERA = "test-cam"


@pytest.fixture
def log() -> LoggerLike:
    return get_logger("test.events")


@pytest.fixture
def metrics() -> MetricsRegistry:
    return MetricsRegistry()


def scene(t_ms: float = 0.0, *, epoch: int = 1, tracker_epoch: int = 1) -> SceneState:
    return (
        SceneStateBuilder(camera_id=CAMERA)
        .with_zone("yard", ZoneKind.RESTRICTED)
        .with_track(
            1,
            ObjectClass.PERSON,
            at=(0.7, 0.6),
            hits=5,
            in_zones=["yard"],
            tracker_epoch=tracker_epoch,
        )
        .with_epoch(epoch)
        .at_time(t_ms, frame_index=int(t_ms // 100))
        .build()
    )


def descriptor(module_id: str = "demo") -> ModuleDescriptor:
    return ModuleDescriptor(
        module_id=module_id,
        event_type=EventType.INTRUSION,
        version="1.0.0",
        requires=frozenset({Capability.DETECTION}),
        optional=frozenset(),
        default_params={},
        description="test module",
    )


def availability(
    module_id: str = "demo", status: Availability = Availability.ACTIVE
) -> ModuleAvailability:
    return ModuleAvailability(
        module_id=module_id,
        event_type=EventType.INTRUSION,
        version="1.0.0",
        camera_id=CAMERA,
        enabled=True,
        status=status,
        requires=frozenset({Capability.DETECTION}),
        optional=frozenset(),
        missing_required=frozenset(),
        missing_optional=frozenset(),
        reason=None,
        description="test module",
    )


class FakeModule:
    """A module whose behaviour a test dictates."""

    def __init__(
        self,
        *,
        module_id: str = "demo",
        signal: float = 0.8,
        raises: BaseException | None = None,
        cost_ms: float = 0.0,
        clock: ManualClock | None = None,
        emit: Sequence[Observation] | None = None,
    ) -> None:
        self._module_id = module_id
        self._signal = signal
        self._raises = raises
        self._cost_ms = cost_ms
        self._clock = clock
        self._emit = emit
        self.bind_count = 0
        self.release_count = 0
        self.observe_count = 0
        self.last_params: dict[str, object] = {}

    @property
    def descriptor(self) -> ModuleDescriptor:
        return descriptor(self._module_id)

    def bind(self, ctx: ModuleContext) -> None:
        self.bind_count += 1
        self.last_params = dict(ctx.params)

    def release(self) -> None:
        self.release_count += 1

    def observe(self, s: SceneState) -> Sequence[Observation]:
        self.observe_count += 1
        if self._clock is not None and self._cost_ms:
            self._clock.advance_ms(self._cost_ms)
        if self._raises is not None:
            raise self._raises
        if self._emit is not None:
            return self._emit
        return (
            Observation(
                camera_id=s.frame_meta.camera_id,
                module_id=self._module_id,
                subject=SubjectKey("track", "1"),
                signal=self._signal,
                t_monotonic_ns=s.frame_meta.t_monotonic_ns,
                facts={},
            ),
        )


def engine(
    modules: Sequence[FakeModule],
    *,
    log: LoggerLike,
    metrics: MetricsRegistry,
    clock: Clock | None = None,
    config: ModulesConfig | None = None,
) -> EventEngine:
    runtimes = [
        ModuleRuntime(
            descriptor=m.descriptor,
            module=m,
            availability=availability(m.descriptor.module_id),
            params={},
        )
        for m in modules
    ]
    return EventEngine(
        camera_id=CAMERA,
        runtimes=runtimes,
        config=config or ModulesConfig(enabled=("demo",)),
        clock=clock or ManualClock(),
        logger=log,
        metrics=metrics,
    )


# ------------------------------------------------------------------ the happy path


def test_observations_from_every_module_are_collected(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    eng = engine([FakeModule(module_id="a"), FakeModule(module_id="b")], log=log, metrics=metrics)
    result = eng.observe(scene())
    assert {o.module_id for o in result.observations} == {"a", "b"}
    assert result.failures == ()


def test_modules_run_in_a_deterministic_order(log: LoggerLike, metrics: MetricsRegistry) -> None:
    eng = engine(
        [FakeModule(module_id="zulu"), FakeModule(module_id="alpha")], log=log, metrics=metrics
    )
    assert [o.module_id for o in eng.observe(scene()).observations] == ["alpha", "zulu"]


def test_a_scene_from_another_camera_is_rejected(log: LoggerLike, metrics: MetricsRegistry) -> None:
    eng = engine([FakeModule()], log=log, metrics=metrics)
    other = SceneStateBuilder(camera_id="other-cam").build()
    with pytest.raises(ModuleError, match="different camera"):
        eng.observe(other)


def test_observation_latency_is_recorded(log: LoggerLike, metrics: MetricsRegistry) -> None:
    clock = ManualClock()
    eng = engine([FakeModule(cost_ms=2.0, clock=clock)], log=log, metrics=metrics, clock=clock)
    result = eng.observe(scene())
    assert result.latency_ms_by_module["demo"] == pytest.approx(2.0)
    summary = metrics.histogram("vigil_stage_latency_ms", stage="observe", camera=CAMERA).summary()
    assert summary.count == 1


# ------------------------------------------------------------------ quarantine (rule 5)


def test_a_raising_module_does_not_reach_the_pipeline(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    eng = engine(
        [FakeModule(module_id="bad", raises=RuntimeError("boom")), FakeModule(module_id="good")],
        log=log,
        metrics=metrics,
    )
    result = eng.observe(scene())
    assert [o.module_id for o in result.observations] == ["good"]
    assert [f.module_id for f in result.failures] == ["bad"]
    assert "boom" in result.failures[0].reason


def test_a_module_is_quarantined_after_the_threshold(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    bad = FakeModule(module_id="bad", raises=RuntimeError("boom"))
    eng = engine([bad], log=log, metrics=metrics, config=ModulesConfig(quarantine_threshold=3))
    for i in range(2):
        result = eng.observe(scene(float(i) * 100.0))
        assert not result.failures[0].quarantined
    result = eng.observe(scene(200.0))
    assert result.failures[0].quarantined
    assert eng.runtime("bad").quarantined


def test_a_quarantined_module_is_skipped_until_the_retry_window(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    bad = FakeModule(module_id="bad", raises=RuntimeError("boom"))
    config = ModulesConfig(quarantine_threshold=1, quarantine_retry_ms=5000)
    eng = engine([bad], log=log, metrics=metrics, config=config)
    eng.observe(scene(0.0))
    calls = bad.observe_count
    result = eng.observe(scene(1000.0))
    assert result.skipped_module_ids == ("bad",)
    assert bad.observe_count == calls  # not called while quarantined
    eng.observe(scene(5000.0))
    assert bad.observe_count == calls + 1  # retried after the window


def test_a_module_that_recovers_leaves_quarantine_cleanly(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    bad = FakeModule(module_id="flaky", raises=RuntimeError("boom"))
    config = ModulesConfig(quarantine_threshold=1, quarantine_retry_ms=1000)
    eng = engine([bad], log=log, metrics=metrics, config=config)
    eng.observe(scene(0.0))
    bad._raises = None  # it starts working again
    result = eng.observe(scene(1000.0))
    assert result.failures == ()
    assert len(result.observations) == 1
    assert not eng.runtime("flaky").quarantined
    assert eng.runtime("flaky").consecutive_errors == 0


def test_a_module_raising_a_bare_exception_is_contained(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    """A plugin may raise anything at all; none of it may take the pipeline down."""
    eng = engine([FakeModule(raises=KeyError("surprise"))], log=log, metrics=metrics)
    result = eng.observe(scene())
    assert "KeyError" in result.failures[0].reason


def test_errors_are_counted_as_metrics_not_swallowed(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    eng = engine([FakeModule(raises=RuntimeError("boom"))], log=log, metrics=metrics)
    eng.observe(scene())
    assert metrics.counter("vigil_stage_errors_total", stage="observe", camera=CAMERA).value == 1.0
    assert metrics.counter("vigil_module_errors_total", camera=CAMERA, module="demo").value == 1.0


# ------------------------------------------------------------------ misattribution


@pytest.mark.parametrize(
    ("field", "bad_value", "match"),
    [
        ("module_id", "someone_else", "attributed to module"),
        ("camera_id", "other-cam", "attributed to camera"),
    ],
)
def test_a_module_cannot_attribute_observations_elsewhere(
    log: LoggerLike, metrics: MetricsRegistry, field: str, bad_value: str, match: str
) -> None:
    """Misattributed evidence would poison another module's or camera's accumulator."""
    base = {
        "camera_id": CAMERA,
        "module_id": "demo",
        "subject": SubjectKey("track", "1"),
        "signal": 0.8,
        "t_monotonic_ns": 0,
        "facts": {},
    }
    forged = Observation(**{**base, field: bad_value})  # type: ignore[arg-type]
    eng = engine([FakeModule(emit=(forged,))], log=log, metrics=metrics)
    result = eng.observe(scene(0.0))
    assert result.observations == ()
    assert match in result.failures[0].reason


def test_a_module_cannot_invent_a_timestamp(log: LoggerLike, metrics: MetricsRegistry) -> None:
    forged = Observation(
        camera_id=CAMERA,
        module_id="demo",
        subject=SubjectKey("track", "1"),
        signal=0.8,
        t_monotonic_ns=999_999_999,
        facts={},
    )
    eng = engine([FakeModule(emit=(forged,))], log=log, metrics=metrics)
    result = eng.observe(scene(0.0))
    assert result.observations == ()
    assert "invent timestamps" in result.failures[0].reason


# ------------------------------------------------------------------ budget (rule 3)


def test_a_module_over_budget_is_counted_then_suspended(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    clock = ManualClock()
    slow = FakeModule(cost_ms=50.0, clock=clock)
    eng = engine(
        [slow],
        log=log,
        metrics=metrics,
        clock=clock,
        config=ModulesConfig(observe_budget_ms=5.0),
    )
    for i in range(2):
        result = eng.observe(scene(float(i) * 1000.0))
        assert result.failures == ()  # counted, not yet fatal
        assert len(result.observations) == 1
    result = eng.observe(scene(2000.0))
    assert result.failures[0].suspended
    assert eng.runtime("demo").suspended
    assert "observe budget" in result.failures[0].reason


def test_a_suspended_module_never_runs_again(log: LoggerLike, metrics: MetricsRegistry) -> None:
    clock = ManualClock()
    slow = FakeModule(cost_ms=50.0, clock=clock)
    eng = engine(
        [slow], log=log, metrics=metrics, clock=clock, config=ModulesConfig(observe_budget_ms=1.0)
    )
    for i in range(3):
        eng.observe(scene(float(i) * 1000.0))
    calls = slow.observe_count
    result = eng.observe(scene(9999.0))
    assert result.skipped_module_ids == ("demo",)
    assert slow.observe_count == calls


def test_an_occasional_overrun_does_not_suspend(log: LoggerLike, metrics: MetricsRegistry) -> None:
    clock = ManualClock()
    module = FakeModule(cost_ms=50.0, clock=clock)
    eng = engine(
        [module],
        log=log,
        metrics=metrics,
        clock=clock,
        config=ModulesConfig(observe_budget_ms=5.0),
    )
    eng.observe(scene(0.0))
    module._cost_ms = 0.0  # back inside budget
    eng.observe(scene(1000.0))
    module._cost_ms = 50.0
    eng.observe(scene(2000.0))
    assert not eng.runtime("demo").suspended
    assert eng.runtime("demo").overruns_total == 2


# ------------------------------------------------------------------ epoch reset (rule 4)


def test_a_stream_epoch_change_rebinds_every_module(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    module = FakeModule()
    eng = engine([module], log=log, metrics=metrics)
    eng.observe(scene(0.0, epoch=1))
    assert module.bind_count == 0  # the engine did not bind it; the test built it bound
    result = eng.observe(scene(100.0, epoch=2))
    assert result.epoch_reset
    assert module.release_count == 1
    assert module.bind_count == 1


def test_a_tracker_epoch_change_rebinds_every_module(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    module = FakeModule()
    eng = engine([module], log=log, metrics=metrics)
    eng.observe(scene(0.0, tracker_epoch=1))
    result = eng.observe(scene(100.0, tracker_epoch=2))
    assert result.epoch_reset
    assert module.bind_count == 1


def test_the_first_frame_is_not_an_epoch_change(log: LoggerLike, metrics: MetricsRegistry) -> None:
    module = FakeModule()
    eng = engine([module], log=log, metrics=metrics)
    assert not eng.observe(scene(0.0)).epoch_reset
    assert module.release_count == 0


def test_a_steady_epoch_does_not_rebind(log: LoggerLike, metrics: MetricsRegistry) -> None:
    module = FakeModule()
    eng = engine([module], log=log, metrics=metrics)
    for i in range(5):
        eng.observe(scene(float(i) * 100.0))
    assert module.bind_count == 0


def test_a_rebind_hands_back_the_resolved_parameters(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    """An epoch change must not silently drop the operator's configuration."""
    module = FakeModule()
    runtime = ModuleRuntime(
        descriptor=module.descriptor,
        module=module,
        availability=availability(),
        params={"base_signal": 0.42},
    )
    eng = EventEngine(
        camera_id=CAMERA,
        runtimes=[runtime],
        config=ModulesConfig(),
        clock=ManualClock(),
        logger=log,
        metrics=metrics,
    )
    eng.observe(scene(0.0, epoch=1))
    eng.observe(scene(100.0, epoch=2))
    assert module.last_params == {"base_signal": 0.42}


def test_a_module_that_fails_to_reset_is_reported_not_fatal(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    class BadReset(FakeModule):
        def bind(self, ctx: ModuleContext) -> None:
            raise RuntimeError("cannot rebind")

    module = BadReset()
    eng = engine([module], log=log, metrics=metrics)
    eng.observe(scene(0.0, epoch=1))
    eng.observe(scene(100.0, epoch=2))  # must not raise
    assert eng.runtime("demo").errors_total == 1


# ------------------------------------------------------------------ shutdown


def test_release_shuts_every_module_down(log: LoggerLike, metrics: MetricsRegistry) -> None:
    modules = [FakeModule(module_id="a"), FakeModule(module_id="b")]
    engine(modules, log=log, metrics=metrics).release()
    assert all(m.release_count == 1 for m in modules)


def test_a_module_that_fails_to_release_does_not_block_shutdown(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    class BadRelease(FakeModule):
        def release(self) -> None:
            raise RuntimeError("stuck")

    engine([BadRelease()], log=log, metrics=metrics).release()  # must not raise


def test_asking_for_an_unbound_module_is_an_explicit_error(
    log: LoggerLike, metrics: MetricsRegistry
) -> None:
    eng = engine([FakeModule()], log=log, metrics=metrics)
    with pytest.raises(ModuleError, match="is not bound on camera"):
        eng.runtime("ghost")


# ------------------------------------------------------------------ binding (06 §5)


def camera_config(**overrides: object) -> CameraConfig:
    return CameraConfig.model_validate(
        {
            "camera_id": CAMERA,
            "source": SourceConfig(kind="synthetic").model_dump(),
            **overrides,
        }
    )


def test_binding_produces_a_runnable_intrusion_module(log: LoggerLike) -> None:
    registry = ModuleRegistry()
    for registration in discover_builtin():
        registry.register(registration)
    report = bind_modules(
        registry=registry,
        camera_id=CAMERA,
        available=FULL_CAPABILITIES,
        config=ModulesConfig(enabled=("intrusion",)),
        logger=log,
    )
    assert report.runnable_ids == ("intrusion",)
    assert report.bind_failures == {}
    assert report.unavailable == ()


def test_binding_refuses_a_module_whose_capabilities_are_absent(log: LoggerLike) -> None:
    report = bind_modules(
        registry=build_registry(include_entry_points=False),
        camera_id=CAMERA,
        available=frozenset({Capability.DETECTION}),
        config=ModulesConfig(enabled=("intrusion",)),
        logger=log,
    )
    assert report.runnable_ids == ()
    # Every discovered module needs TRACKING, so every one of them is unavailable here. The
    # enabled one is what matters: it must say which capability is missing.
    intrusion = next(a for a in report.unavailable if a.module_id == "intrusion")
    assert intrusion.reason is not None
    assert "zones" in intrusion.reason


def test_binding_reports_a_module_whose_parameters_do_not_validate(log: LoggerLike) -> None:
    """A bad parameter file makes the module unavailable with the error as its reason — it does
    not stop the camera, and it does not leave the module looking operational."""
    report = bind_modules(
        registry=build_registry(include_entry_points=False),
        camera_id=CAMERA,
        available=FULL_CAPABILITIES,
        config=ModulesConfig(enabled=("intrusion",)),
        logger=log,
        params_by_module={"intrusion": {"not_a_real_key": 1}},
    )
    assert report.runnable_ids == ()
    assert "intrusion" in report.bind_failures
    assert report.unavailable[0].status is Availability.UNAVAILABLE
    assert "failed to bind" in (report.unavailable[0].reason or "")


def test_per_camera_parameters_override_the_module_file(log: LoggerLike) -> None:
    registry = build_registry(include_entry_points=False)
    params = resolve_params(
        registry,
        "intrusion",
        file_params={"base_signal": 0.6},
        camera=camera_config(module_params={"intrusion": {"base_signal": 0.4}}),
    )
    assert params["base_signal"] == pytest.approx(0.4)


def test_module_file_parameters_override_descriptor_defaults(log: LoggerLike) -> None:
    params = resolve_params(
        build_registry(include_entry_points=False),
        "intrusion",
        file_params={"base_signal": 0.55},
        camera=None,
    )
    assert params["base_signal"] == pytest.approx(0.55)
    assert params["min_track_hits"] == 3  # untouched default survives


def test_required_capabilities_reports_what_the_enabled_set_needs() -> None:
    needed = required_capabilities(build_registry(include_entry_points=False), ["intrusion"])
    assert needed == frozenset({Capability.DETECTION, Capability.TRACKING, Capability.ZONES})


def test_binding_a_degraded_module_keeps_it_runnable_with_caveats(log: LoggerLike) -> None:
    report = bind_modules(
        registry=build_registry(include_entry_points=False),
        camera_id=CAMERA,
        available=frozenset({Capability.DETECTION, Capability.TRACKING, Capability.ZONES}),
        config=ModulesConfig(enabled=("intrusion",)),
        logger=log,
    )
    assert report.runnable_ids == ("intrusion",)
    assert report.caveats == ("intrusion: running without velocity",)
