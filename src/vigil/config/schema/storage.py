"""Storage settings (05 §5, D-006): SQLite in WAL mode, plus filesystem evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from vigil.config.schema.base import ConfigModel

Synchronous = Literal["normal", "full"]


class StorageConfig(ConfigModel):
    """Everything about the incident store. Defaults favour durability over throughput.

    The write load is a handful of rows per incident plus one batched metrics insert every
    `observability.metric_flush_s`, so there is nothing to gain by trading away durability —
    and an incident record lost to a power cut is the one record that mattered.
    """

    enabled: bool = True
    # Default: <db_dir>/vigil.sqlite3. An explicit path is resolved like every other path.
    database_file: Path | None = None
    # How long a writer waits for SQLite's write lock before reporting failure. WAL allows one
    # writer and many readers, so this is only ever contended between the response worker and
    # the metrics flush.
    busy_timeout_ms: int = Field(default=5000, ge=100)
    # `full` fsyncs the WAL on every commit, which survives a power cut; `normal` survives a
    # process crash but may lose the most recent commits. Both are safe against corruption.
    synchronous: Synchronous = "full"
    # Negative `cache_size` in SQLite means kibibytes rather than pages.
    page_cache_kb: int = Field(default=8192, ge=256)
    # How long the analysis thread waits to hand a decided event to the response worker before
    # spilling it to the journal. It must be short: that thread owns a camera's detection.
    handoff_timeout_ms: int = Field(default=250, ge=0)
    # Pagination ceiling for incident and candidate listings, so one API call cannot ask for
    # the whole table.
    max_page_size: int = Field(default=200, ge=1, le=1000)
    # An open incident's `last_observed` grows while evidence keeps arriving (06 §9), but it is
    # not worth an UPDATE per analysed frame: at 20 fps that is 20 writes a second per open
    # situation, to move a timestamp by 50 ms. This throttles it to one write per interval.
    incident_extend_interval_ms: int = Field(default=1000, ge=0)
