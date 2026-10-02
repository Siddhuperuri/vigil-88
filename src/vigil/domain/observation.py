"""Observations: what a module emits (D-003). A module never emits an Incident."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from vigil.core.ids import validate_camera_id
from vigil.domain._validate import (
    frozen_mapping,
    require_non_empty,
    require_non_negative,
    require_ratio,
)

SubjectKind = Literal["track", "zone", "region", "camera"]
FactValue = float | str | bool


@dataclass(frozen=True, slots=True)
class SubjectKey:
    kind: SubjectKind
    value: str

    def __post_init__(self) -> None:
        if self.kind != "camera":
            require_non_empty("SubjectKey.value", self.value)

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


@dataclass(frozen=True, slots=True)
class Observation:
    """This frame's support for a hypothesis. NOT a probability that an incident is occurring."""

    camera_id: str
    module_id: str
    subject: SubjectKey
    signal: float
    t_monotonic_ns: int
    facts: Mapping[str, FactValue]

    def __post_init__(self) -> None:
        validate_camera_id(self.camera_id)
        require_non_empty("Observation.module_id", self.module_id)
        require_ratio("Observation.signal", self.signal)
        require_non_negative("t_monotonic_ns", self.t_monotonic_ns)
        object.__setattr__(self, "facts", frozen_mapping(self.facts))
