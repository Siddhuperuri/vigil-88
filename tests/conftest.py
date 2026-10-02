"""Shared fixtures (09 §12). Time is manual, randomness seeded, the filesystem is tmp_path."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from vigil.config.loader import LoadedConfig, load_settings
from vigil.core.clock import ManualClock
from vigil.observability.logging import shutdown_logging

REPO_ROOT = Path(__file__).resolve().parents[1]

# Fast reconnect/shutdown settings so threaded tests finish in milliseconds.
FAST_OVERRIDES: dict[str, object] = {
    "ingest.reconnect_base_ms": 1,
    "ingest.reconnect_max_ms": 5,
    "ingest.glitch_wait_ms": 0,
    "ingest.watchdog_interval_ms": 50,
    "pipeline.worker_idle_wait_ms": 1,
    "pipeline.shutdown_timeout_ms": 2000,
    "observability.sample_interval_ms": 100,
    "logging.console": False,
    "logging.file_enabled": False,
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--webcam", action="store_true", help="run tests that need a physical webcam")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--webcam"):
        return
    skip = pytest.mark.skip(reason="needs a physical webcam: run with --webcam")
    for item in items:
        if item.get_closest_marker("webcam") is not None:
            item.add_marker(skip)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., LoadedConfig]:
    """Build a LoadedConfig rooted in tmp_path. No real env vars, no .env, no repo config."""

    def build(
        overrides: dict[str, object] | None = None, env: dict[str, str] | None = None
    ) -> LoadedConfig:
        merged = {**FAST_OVERRIDES, **(overrides or {})}
        return load_settings(
            config_dir=tmp_path / "config", env=env or {}, overrides=merged, use_dotenv=False
        )

    return build


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()
