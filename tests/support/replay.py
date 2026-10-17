"""Replaying a scripted sequence of SceneStates through the full P3 chain.

This is the integration harness. It is deliberately **not** a pipeline-with-a-mock-detector,
because P2's tracker and scene analyzer do not exist yet (see docs/LIMITATIONS.md): it drives
the chain at the `SceneState` boundary, which is the seam the architecture defines for exactly
this purpose — `events/` receives a scene and never knows what produced it (01 §5).

Everything downstream of that boundary is the real thing: the real registry, the real intrusion
module, the real accumulator, the real eleven validators, the real severity engine, wired by the
same `build_camera_pipeline` the application uses.

Time is explicit and comes from the scenes. Nothing sleeps.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from tests.support.scenes import FULL_CAPABILITIES, SceneStateBuilder
from vigil.config.schema.camera import CameraConfig, SourceConfig
from vigil.config.settings import Settings
from vigil.core.capabilities import Capability
from vigil.core.clock import ManualClock
from vigil.core.ids import UlidFactory
from vigil.domain.camera import CameraHealth
from vigil.domain.event import CandidateRecord
from vigil.domain.scene import SceneState
from vigil.events.binding import BindingReport
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.analysis import AnalysisPipeline, AnalysisTick, ScoredEvent
from vigil.pipeline.events import build_camera_pipeline, build_event_chain
from vigil.verification.validators.context import Mute

CAMERA = "test-cam"


def camera_config(camera_id: str = CAMERA, **overrides: object) -> CameraConfig:
    return CameraConfig.model_validate(
        {
            "camera_id": camera_id,
            "source": SourceConfig(kind="synthetic").model_dump(),
            "target_inference_fps": 10.0,
            **overrides,
        }
    )


def settings_for(**overrides: object) -> Settings:
    """Settings with intrusion enabled. Overrides are dotted paths, e.g. temporal.half_life_ms."""
    data: dict[str, object] = {"modules": {"enabled": ["intrusion"]}}
    for path, value in overrides.items():
        section, _, key = path.partition(".")
        if not key:
            data[section] = value
            continue
        existing = data.setdefault(section, {})
        assert isinstance(existing, dict)
        existing[key] = value
    return Settings.model_validate(data)


@dataclass(slots=True)
class ReplayResult:
    """Everything a scenario produced, in the order it happened."""

    ticks: list[AnalysisTick] = field(default_factory=list)

    @property
    def scored(self) -> list[ScoredEvent]:
        return [s for tick in self.ticks for s in tick.scored]

    @property
    def records(self) -> list[CandidateRecord]:
        return [r for tick in self.ticks for r in tick.records]

    @property
    def rejected(self) -> list[CandidateRecord]:
        return [r for tick in self.ticks for r in tick.rejected]

    @property
    def observations_total(self) -> int:
        return sum(len(tick.observations) for tick in self.ticks)

    @property
    def released(self) -> list[object]:
        return [s for tick in self.ticks for s in tick.released]

    @property
    def epoch_resets(self) -> int:
        return sum(1 for tick in self.ticks if tick.epoch_reset)

    @property
    def peak_active_candidates(self) -> int:
        return max((tick.active_candidates for tick in self.ticks), default=0)

    def fingerprint(self) -> tuple[object, ...]:
        """A comparable summary that excludes ULIDs and wall timestamps.

        09 §8's reproducibility gate is "byte-identical bar ULIDs and wall timestamps", so those
        are exactly what this leaves out — everything else must match on a replay.
        """
        return tuple(
            (
                tick.t_monotonic_ns,
                tuple(
                    (o.module_id, str(o.subject), round(o.signal, 9), o.t_monotonic_ns)
                    for o in tick.observations
                ),
                tuple(
                    (
                        str(s.candidate.subject),
                        s.candidate.event_type.value,
                        round(s.event.confidence_ratio, 9),
                        round(s.assessment.score, 9),
                        s.assessment.severity.name,
                        round(s.detection_latency_ms, 6),
                    )
                    for s in tick.scored
                ),
                tuple((str(r.subject), r.outcome.value, r.rejection_reason) for r in tick.records),
                tuple(str(s) for s in tick.released),
                tick.epoch_reset,
                tick.active_candidates,
            )
            for tick in self.ticks
        )


class Replay:
    """One camera's chain, driven by scenes a test supplies."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        camera: CameraConfig | None = None,
        available: frozenset[Capability] = FULL_CAPABILITIES,
        ulid_seed: int = 42,
        camera_health: CameraHealth | None = None,
        mutes: Sequence[Mute] = (),
    ) -> None:
        self.settings = settings or settings_for()
        self.camera = camera or camera_config()
        self.metrics = MetricsRegistry()
        self.logger = get_logger("test.replay")
        self.clock = ManualClock()
        chain = build_event_chain(settings=self.settings, logger=self.logger, metrics=self.metrics)
        self.chain = chain
        pipeline, report = build_camera_pipeline(
            chain=chain,
            settings=self.settings,
            camera=self.camera,
            available=available,
            clock=self.clock,
            ulids=UlidFactory(ManualClock(), random.Random(ulid_seed)),
            logger=self.logger,
            metrics=self.metrics,
        )
        self.pipeline: AnalysisPipeline = pipeline
        self.report: BindingReport = report
        if camera_health is not None:
            pipeline.set_camera_health(camera_health)
        if mutes:
            pipeline.set_mutes(mutes)

    def run(self, scenes: Iterable[SceneState]) -> ReplayResult:
        result = ReplayResult()
        for scene in scenes:
            result.ticks.append(self.pipeline.process(scene))
        return result


def walk(
    *,
    inside_from_ms: float | None = 0.0,
    inside_until_ms: float | None = None,
    duration_ms: float = 20_000.0,
    step_ms: float = 100.0,
    camera_id: str = CAMERA,
    track_id: int = 1,
    zone_id: str = "yard",
    criticality: int = 3,
    inside_at: tuple[float, float] = (0.7, 0.6),
    outside_at: tuple[float, float] = (0.1, 0.6),
    capabilities: frozenset[Capability] = FULL_CAPABILITIES,
    stability_ratio: float = 1.0,
    hits_cap: int | None = None,
    stream_epoch: int = 1,
    tracker_epoch: int = 1,
    velocity_px_s: tuple[float, float] | None = None,
) -> Iterator[SceneState]:
    """One person who is inside `zone_id` between the two times and outside it otherwise.

    The track exists throughout, which is what a real tracker would report: a person who walks
    out of a zone is still a tracked person. Dwell accumulates only while inside.
    """
    dwell_ms = 0.0
    frames = int(duration_ms / step_ms) + 1
    for index in range(frames):
        t_ms = index * step_ms
        after_start = inside_from_ms is not None and t_ms >= inside_from_ms
        before_end = inside_until_ms is None or t_ms < inside_until_ms
        inside = after_start and before_end
        dwell_ms = dwell_ms + step_ms if inside else 0.0
        hits = index + 1 if hits_cap is None else min(index + 1, hits_cap)
        builder = (
            SceneStateBuilder(camera_id=camera_id)
            .with_zone(zone_id, criticality=criticality)
            .with_track(
                track_id,
                at=inside_at if inside else outside_at,
                hits=hits,
                age_frames=index + 1,
                in_zones=[zone_id] if inside else [],
                dwell_ms={zone_id: dwell_ms},
                first_seen_ms=0.0,
                velocity_px_s=velocity_px_s,
            )
            .with_capabilities(capabilities)
            .with_stability(stability_ratio)
            .with_epoch(stream_epoch)
            .at_time(t_ms, frame_index=index)
        )
        for spec in builder._tracks:
            spec.tracker_epoch = tracker_epoch
        yield builder.build()
