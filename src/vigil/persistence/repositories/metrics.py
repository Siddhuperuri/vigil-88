"""System metrics and camera records (05 §5).

Metrics are written in **batches**, in one transaction, at `observability.metric_flush_s`. One
INSERT per second per metric would dominate the write load for no gain, and would put a commit
on the sampler's cadence rather than on a cadence we chose.

Retention downsamples before deleting (05 §8): rows past `retention.metric_days` are collapsed
to hourly averages and only then removed, so a month-old trend survives at a fraction of the
rows instead of vanishing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from vigil.config.schema.camera import CameraConfig
from vigil.persistence.database import Database
from vigil.persistence.mappers import dumps, from_utc_text, to_utc_text

METRIC_COLUMNS = (
    "t_utc",
    "cpu_percent",
    "memory_used_mb",
    "process_rss_mb",
    "gpu_utilization_percent",
    "gpu_memory_used_mb",
    "gpu_temperature_c",
    "pipeline_fps",
    "inference_latency_p50_ms",
    "inference_latency_p95_ms",
    "queue_depths_json",
    "frames_dropped_total",
)


@dataclass(frozen=True, slots=True)
class MetricSample:
    """What one sampler tick produced. Every GPU field is optional: the UI must render
    "unavailable", never 0 (05 §4)."""

    t_wall_utc: datetime
    cpu_percent: float
    memory_used_mb: float
    process_rss_mb: float
    gpu_utilization_percent: float | None
    gpu_memory_used_mb: float | None
    gpu_temperature_c: float | None
    pipeline_fps: float | None
    inference_latency_p50_ms: float | None
    inference_latency_p95_ms: float | None
    queue_depths: Mapping[str, int]
    frames_dropped_total: int

    def to_row(self) -> dict[str, object]:
        return {
            "t_utc": to_utc_text(self.t_wall_utc),
            "cpu_percent": self.cpu_percent,
            "memory_used_mb": self.memory_used_mb,
            "process_rss_mb": self.process_rss_mb,
            "gpu_utilization_percent": self.gpu_utilization_percent,
            "gpu_memory_used_mb": self.gpu_memory_used_mb,
            "gpu_temperature_c": self.gpu_temperature_c,
            "pipeline_fps": self.pipeline_fps,
            "inference_latency_p50_ms": self.inference_latency_p50_ms,
            "inference_latency_p95_ms": self.inference_latency_p95_ms,
            "queue_depths_json": dumps(dict(self.queue_depths)),
            "frames_dropped_total": self.frames_dropped_total,
        }


class MetricRepository:
    __slots__ = ("_db",)

    def __init__(self, database: Database) -> None:
        self._db = database

    def write_batch(self, samples: Sequence[MetricSample]) -> int:
        if not samples:
            return 0
        placeholders = ", ".join(f":{c}" for c in METRIC_COLUMNS)
        with self._db.write(operation="insert_metrics") as connection:
            connection.executemany(
                f"INSERT INTO system_metrics ({', '.join(METRIC_COLUMNS)}) "
                f"VALUES ({placeholders}) ON CONFLICT (t_utc) DO NOTHING",
                [s.to_row() for s in samples],
            )
        return len(samples)

    def series(
        self, *, since_wall_utc: datetime, until_wall_utc: datetime, limit: int
    ) -> tuple[MetricSample, ...]:
        rows = self._db.query(
            f"SELECT {', '.join(METRIC_COLUMNS)} FROM system_metrics "
            "WHERE t_utc >= ? AND t_utc < ? ORDER BY t_utc DESC LIMIT ?",
            (to_utc_text(since_wall_utc), to_utc_text(until_wall_utc), limit),
        )
        import json

        return tuple(
            MetricSample(
                t_wall_utc=from_utc_text(r["t_utc"]),
                cpu_percent=float(r["cpu_percent"]),
                memory_used_mb=float(r["memory_used_mb"]),
                process_rss_mb=float(r["process_rss_mb"]),
                gpu_utilization_percent=_optional_float(r["gpu_utilization_percent"]),
                gpu_memory_used_mb=_optional_float(r["gpu_memory_used_mb"]),
                gpu_temperature_c=_optional_float(r["gpu_temperature_c"]),
                pipeline_fps=_optional_float(r["pipeline_fps"]),
                inference_latency_p50_ms=_optional_float(r["inference_latency_p50_ms"]),
                inference_latency_p95_ms=_optional_float(r["inference_latency_p95_ms"]),
                queue_depths=json.loads(r["queue_depths_json"]),
                frames_dropped_total=int(r["frames_dropped_total"]),
            )
            for r in rows
        )

    def downsample_and_delete_older_than(self, cutoff_wall_utc: datetime) -> tuple[int, int]:
        """Collapse pre-cutoff rows to hourly averages, then delete the originals.

        Returns (hourly rows written, original rows deleted). One transaction, so a crash
        between the two halves cannot lose the detail without having written the summary.
        """
        cutoff = to_utc_text(cutoff_wall_utc)
        with self._db.write(operation="downsample_metrics") as connection:
            aggregates = connection.execute(
                """
                SELECT substr(t_utc, 1, 13) || ':00:00+00:00' AS hour_utc,
                       AVG(cpu_percent)              AS cpu_percent,
                       AVG(memory_used_mb)           AS memory_used_mb,
                       AVG(process_rss_mb)           AS process_rss_mb,
                       AVG(gpu_utilization_percent)  AS gpu_utilization_percent,
                       AVG(gpu_memory_used_mb)       AS gpu_memory_used_mb,
                       AVG(gpu_temperature_c)        AS gpu_temperature_c,
                       AVG(pipeline_fps)             AS pipeline_fps,
                       AVG(inference_latency_p50_ms) AS inference_latency_p50_ms,
                       MAX(inference_latency_p95_ms) AS inference_latency_p95_ms,
                       MAX(frames_dropped_total)     AS frames_dropped_total,
                       COUNT(*)                      AS samples
                  FROM system_metrics
                 WHERE t_utc < ?
                 GROUP BY hour_utc
                 HAVING samples > 1
                """,
                (cutoff,),
            ).fetchall()
            for row in aggregates:
                connection.execute(
                    f"INSERT INTO system_metrics ({', '.join(METRIC_COLUMNS)}) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (t_utc) DO NOTHING",
                    (
                        row["hour_utc"],
                        row["cpu_percent"],
                        row["memory_used_mb"],
                        row["process_rss_mb"],
                        row["gpu_utilization_percent"],
                        row["gpu_memory_used_mb"],
                        row["gpu_temperature_c"],
                        row["pipeline_fps"],
                        row["inference_latency_p50_ms"],
                        row["inference_latency_p95_ms"],
                        dumps({}),
                        row["frames_dropped_total"],
                    ),
                )
            hours = {str(r["hour_utc"]) for r in aggregates}
            deleted = 0
            if hours:
                placeholders = ", ".join("?" for _ in hours)
                cursor = connection.execute(
                    f"DELETE FROM system_metrics WHERE t_utc < ? AND t_utc NOT IN ({placeholders})",
                    (cutoff, *sorted(hours)),
                )
                deleted = max(cursor.rowcount, 0)
                cursor.close()
        return (len(aggregates), deleted)

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM system_metrics") or 0)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


class CameraRepository:
    """Cameras are configuration, mirrored into the database so an incident's camera is
    describable after that camera is renamed or removed (05 §4)."""

    __slots__ = ("_db",)

    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert_all(self, cameras: Sequence[CameraConfig], *, now_wall_utc: datetime) -> int:
        if not cameras:
            return 0
        now = to_utc_text(now_wall_utc)
        rows = [
            {
                "camera_id": c.camera_id,
                "name": c.display_name,
                "source_kind": c.source.kind,
                # The locator never carries credentials: the config validator refuses them and
                # they are resolved from the environment at connection time (01 §10).
                "source_locator": c.source.url
                or (str(c.source.path) if c.source.path is not None else None),
                "location": c.location,
                "priority": c.priority,
                "enabled": int(c.enabled),
                "config_json": dumps(
                    {
                        "target_inference_fps": c.target_inference_fps,
                        "zones_ref": c.zones_ref,
                        "tags": list(c.tags),
                        "trust_ratio": c.trust_ratio,
                    }
                ),
                "created_utc": now,
                "updated_utc": now,
            }
            for c in cameras
        ]
        with self._db.write(operation="upsert_cameras") as connection:
            connection.executemany(
                """
                INSERT INTO cameras (camera_id, name, source_kind, source_locator, location,
                                     priority, enabled, config_json, created_utc, updated_utc)
                VALUES (:camera_id, :name, :source_kind, :source_locator, :location,
                        :priority, :enabled, :config_json, :created_utc, :updated_utc)
                ON CONFLICT (camera_id) DO UPDATE SET
                    name = excluded.name,
                    source_kind = excluded.source_kind,
                    source_locator = excluded.source_locator,
                    location = excluded.location,
                    priority = excluded.priority,
                    enabled = excluded.enabled,
                    config_json = excluded.config_json,
                    updated_utc = excluded.updated_utc
                """,
                rows,
            )
        return len(rows)

    def location_of(self, camera_id: str) -> str | None:
        value = self._db.scalar("SELECT location FROM cameras WHERE camera_id = ?", (camera_id,))
        return None if value is None else str(value)

    def ids(self) -> tuple[str, ...]:
        rows = self._db.query("SELECT camera_id FROM cameras ORDER BY camera_id")
        return tuple(str(r["camera_id"]) for r in rows)
