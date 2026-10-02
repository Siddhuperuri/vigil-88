"""Detection-module plugin contract (04 §3.5, D-003).

A module returns Observations and nothing else. It never creates incidents, writes
evidence, sends alerts, reads a clock or touches the network or filesystem.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.core.capabilities import Capability
    from vigil.core.protocols.logger import LoggerLike
    from vigil.domain.enums import EventType
    from vigil.domain.observation import Observation
    from vigil.domain.scene import SceneState


@dataclass(frozen=True, slots=True)
class ModuleDescriptor:
    module_id: str
    event_type: EventType
    version: str
    requires: frozenset[Capability]
    optional: frozenset[Capability]
    default_params: Mapping[str, object]
    description: str


@dataclass(frozen=True, slots=True)
class ModuleContext:
    camera_id: str
    params: Mapping[str, object]
    logger: LoggerLike


class DetectionModule(Protocol):
    @property
    def descriptor(self) -> ModuleDescriptor: ...

    def bind(self, ctx: ModuleContext) -> None: ...

    def observe(self, scene: SceneState) -> Sequence[Observation]: ...

    def release(self) -> None: ...
