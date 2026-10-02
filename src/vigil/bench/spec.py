"""What a benchmark run is: one frozen, validated, reproducible description."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vigil.core.errors import ConfigError

SUSTAINED_MIN_S = 120.0  # below this a run is a screening run, not a sustained benchmark
MIN_DURATION_S = 5.0
SourceName = Literal["synthetic", "webcam"]


@dataclass(frozen=True, slots=True)
class BenchSpec:
    name: str
    detector: str  # model name, e.g. yolox_s
    device: Literal["auto", "cuda", "cpu"]
    source: SourceName
    cameras: int = 1
    width_px: int = 640
    height_px: int = 480
    source_fps: float = 30.0
    infer_fps: float = 30.0  # target inference rate PER CAMERA (the scheduler's token rate)
    duration_s: float = SUSTAINED_MIN_S
    window_s: float = 30.0  # the "first N seconds" and "last N seconds" comparison windows
    stream: bool = True  # an in-process viewer drains an annotated stream, as a browser would

    def validate(self) -> None:
        problems: list[str] = []
        if self.source == "webcam" and self.cameras != 1:
            problems.append("the webcam source supports exactly one camera")
        if not 1 <= self.cameras <= 16:
            problems.append("cameras must be between 1 and 16")
        if self.duration_s < MIN_DURATION_S:
            problems.append(f"duration must be at least {MIN_DURATION_S:g} s")
        if self.window_s * 2 > self.duration_s:
            problems.append("the first/last comparison windows would overlap: shorten window_s")
        if min(self.source_fps, self.infer_fps) <= 0:
            problems.append("source_fps and infer_fps must be positive")
        if problems:
            raise ConfigError("invalid benchmark specification", issues=problems)

    @property
    def sustained(self) -> bool:
        return self.duration_s >= SUSTAINED_MIN_S
