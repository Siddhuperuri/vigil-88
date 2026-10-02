"""Paths, logging and observability settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from vigil.config.schema.base import ConfigModel

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ConsoleFormat = Literal["auto", "pretty", "plain", "json"]


class PathsConfig(ConfigModel):
    """Everything derives from `data_dir`. Relative paths resolve against the directory
    that contains `config/`, never against the process working directory."""

    data_dir: Path = Path("var")
    models_dir: Path | None = None
    logs_dir: Path | None = None
    evidence_dir: Path | None = None
    db_dir: Path | None = None
    bench_dir: Path | None = None


class LoggingConfig(ConfigModel):
    level: LogLevel = "INFO"
    console: bool = True
    console_format: ConsoleFormat = "auto"
    file_enabled: bool = True
    json_file: Path | None = None  # default: <logs_dir>/vigil.jsonl
    rotate_mb: int = Field(default=64, ge=1)
    retain_files: int = Field(default=10, ge=0)


class ObservabilityConfig(ConfigModel):
    sample_interval_ms: int = Field(default=1000, ge=100)
    series_window_s: int = Field(default=300, ge=10)
    histogram_samples: int = Field(default=2048, ge=64)
    fps_window_s: float = Field(default=5.0, gt=0)
