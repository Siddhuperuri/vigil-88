"""Capturing an incident's evidence, including the footage that comes after it (05 §6).

The timing problem this solves: an incident must be recorded the instant it is confirmed, but
its post-roll does not exist yet. Writing evidence immediately would mean no post-roll; waiting
to write the incident would mean a confirmed incident that is not in the database.

So the two are separated. The incident is persisted at once with `evidence_status = pending`,
and a **capture is held open** until its post-roll window closes -- typically ten seconds -- at
which point everything is written in one session and the status becomes `complete` or `partial`.
That is exactly the CONFIRMED-then-evidence-complete sequence of 06 §9, and it is why
`EvidenceStatus.PENDING` exists.

What is captured at confirmation versus at completion matters, and getting it backwards would be
a subtle lie:

  * the **snapshots are taken at confirmation** and held as bytes, because by completion time
    the latest frame is ten seconds past the event and would show an empty scene;
  * the **clips are sliced at completion** from the camera's ring, which by then spans both
    sides of the event;
  * the **scene and explanation** are the values from the deciding frame, carried on the tick.

Bounded, like every other queue here (01 §9): at most `evidence.max_concurrent_captures` are
open, and beyond that the oldest is completed early with a shorter post-roll. That is
degradation, and it is counted -- not loss.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from vigil.config.schema.pipeline import EvidenceConfig
from vigil.core.clock import Clock
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import MS_PER_S, NS_PER_MS, ms_to_ns
from vigil.domain.enums import EvidenceKind
from vigil.domain.incident import IncidentExplanation
from vigil.domain.scene import SceneState
from vigil.ingest.buffers import EncodedFrame, PreRollBuffer
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.annotate import OverlayOptions, RenderError, render_jpeg
from vigil.pipeline.evidence_media import mux_clip
from vigil.pipeline.results import LatestDetections
from vigil.response.evidence import (
    JPEG_MEDIA_TYPE,
    MP4_MEDIA_TYPE,
    EvidenceReport,
    EvidenceStatus,
    EvidenceWriter,
    IncidentEvidence,
)

SNAPSHOT_RAW = "snapshot_raw.jpg"
SNAPSHOT_ANNOTATED = "snapshot_annotated.jpg"
CLIP_PRE = "clip_pre.mp4"
CLIP_POST = "clip_post.mp4"
TRACKS = "tracks.json"
SCENE = "scene.json"
EXPLANATION = "explanation.json"
ANNOTATED = OverlayOptions(boxes=True, labels=True, info=True)


@dataclass(slots=True)
class PendingCapture:
    """One incident's capture, open until its post-roll window closes."""

    incident_id: str
    display_ref: str
    camera_id: str
    first_observed_ns: int
    confirmed_ns: int
    confirmed_wall_utc: datetime
    deadline_ns: int
    scene: SceneState | None
    explanation: IncidentExplanation
    involved_track_ids: frozenset[int]
    raw_jpeg: bytes | None = None
    annotated_jpeg: bytes | None = None
    snapshot_frame_index: int | None = None
    # Why a snapshot is missing, kept next to the thing it explains rather than pooled with the
    # general notes: `fail()` needs the specific reason, not whichever note happened to be first.
    raw_reason: str | None = None
    annotated_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def is_due(self, now_ns: int) -> bool:
        return now_ns >= self.deadline_ns


class EvidenceCapture:
    """Owns the open captures. Driven by the response worker's thread, so it locks only the
    pending map -- which the API also reads, to report how many captures are in flight."""

    __slots__ = (
        "_buffers",
        "_clock",
        "_config",
        "_lock",
        "_log",
        "_metrics",
        "_pending",
        "_results",
        "_writer",
    )

    def __init__(
        self,
        *,
        writer: EvidenceWriter,
        buffers: Mapping[str, PreRollBuffer],
        results: LatestDetections,
        config: EvidenceConfig,
        clock: Clock,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._writer = writer
        self._buffers = dict(buffers)
        self._results = results
        self._config = config
        self._clock = clock
        self._log = logger.bind(component="evidence_capture")
        self._metrics = metrics
        self._lock = threading.Lock()
        self._pending: dict[str, PendingCapture] = {}

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def enabled(self) -> bool:
        return self._writer.enabled and self._config.enabled

    # ---------------------------------------------------------------- opening

    def begin(
        self,
        *,
        incident_id: str,
        display_ref: str,
        camera_id: str,
        first_observed_ns: int,
        confirmed_ns: int,
        confirmed_wall_utc: datetime,
        scene: SceneState | None,
        explanation: IncidentExplanation,
        involved_track_ids: frozenset[int],
    ) -> PendingCapture | None:
        """Take the snapshots now and hold the capture open. None when evidence is disabled."""
        if not self.enabled:
            return None
        postroll_ns = ms_to_ns(self._config.postroll_seconds * MS_PER_S)
        capture = PendingCapture(
            incident_id=incident_id,
            display_ref=display_ref,
            camera_id=camera_id,
            first_observed_ns=first_observed_ns,
            confirmed_ns=confirmed_ns,
            confirmed_wall_utc=confirmed_wall_utc,
            deadline_ns=confirmed_ns + postroll_ns,
            scene=scene,
            explanation=explanation,
            involved_track_ids=involved_track_ids,
        )
        self._take_snapshots(capture)
        with self._lock:
            self._pending[incident_id] = capture
        self._metrics.gauge("vigil_evidence_captures_open").set(float(len(self._pending)))
        return capture

    def _take_snapshots(self, capture: PendingCapture) -> None:
        """Raw from the ring (the right frame, already encoded); annotated from the live result.

        The raw snapshot comes from the pre-roll ring because that is the frame nearest the
        decision; re-encoding the current frame would give a *later* one and quietly mislabel it.
        """
        buffer = self._buffers.get(capture.camera_id)
        if buffer is not None:
            nearest = self._nearest(buffer, capture.confirmed_ns)
            if nearest is not None:
                capture.raw_jpeg = nearest.jpeg
                capture.snapshot_frame_index = nearest.meta.frame_index
        if capture.raw_jpeg is None:
            capture.raw_reason = (
                "no buffered frame was available at confirmation, so there is no raw snapshot"
            )
        analysed = self._results.latest(capture.camera_id)
        if analysed is None:
            capture.annotated_reason = "no analysed frame was available to annotate"
            return
        try:
            capture.annotated_jpeg = render_jpeg(
                analysed, ANNOTATED, jpeg_quality=self._config.snapshot_quality, age_ms=None
            )
        except RenderError as exc:
            capture.annotated_reason = (
                f"the annotated snapshot could not be rendered: {exc.message}"
            )

    @staticmethod
    def _nearest(buffer: PreRollBuffer, t_ns: int) -> EncodedFrame | None:
        frames = buffer.window(since_ns=0, until_ns=t_ns)
        if frames:
            return frames[-1]
        return buffer.latest()

    # ---------------------------------------------------------------- completing

    def due(self, now_ns: int) -> tuple[PendingCapture, ...]:
        """Captures whose post-roll window has closed, oldest first."""
        with self._lock:
            ready = [c for c in self._pending.values() if c.is_due(now_ns)]
            over = len(self._pending) - self._config.max_concurrent_captures
            if over > 0:
                oldest = sorted(self._pending.values(), key=lambda c: c.confirmed_ns)
                for capture in oldest[:over]:
                    if capture not in ready:
                        capture.notes.append(
                            "completed early because too many captures were open; the post-roll "
                            "is shorter than configured"
                        )
                        ready.append(capture)
                        self._metrics.counter("vigil_evidence_captures_early_total").inc()
        return tuple(sorted(ready, key=lambda c: c.confirmed_ns))

    def take_all(self) -> tuple[PendingCapture, ...]:
        """Every open capture, for flushing at shutdown. Post-roll will be short; it is noted."""
        with self._lock:
            captures = tuple(sorted(self._pending.values(), key=lambda c: c.confirmed_ns))
            self._pending.clear()
        for capture in captures:
            capture.notes.append(
                "the application shut down before the post-roll window closed, so this clip is "
                "shorter than configured"
            )
        return captures

    def complete(self, capture: PendingCapture) -> EvidenceReport:
        """Write everything for one capture and close it. Never raises."""
        with self._lock:
            self._pending.pop(capture.incident_id, None)
        self._metrics.gauge("vigil_evidence_captures_open").set(float(len(self._pending)))

        session = self._writer.open(
            camera_id=capture.camera_id,
            incident_id=capture.incident_id,
            confirmed_wall_utc=capture.confirmed_wall_utc,
        )
        if session is None:
            return EvidenceReport(
                incident_id=capture.incident_id,
                relative_dir="",
                records=(),
                failures=(),
                status=EvidenceStatus.UNAVAILABLE,
            )

        requested = 0
        if capture.raw_jpeg is not None:
            session.write(
                EvidenceKind.SNAPSHOT_RAW,
                SNAPSHOT_RAW,
                capture.raw_jpeg,
                media_type=JPEG_MEDIA_TYPE,
            )
        else:
            session.fail(
                EvidenceKind.SNAPSHOT_RAW,
                SNAPSHOT_RAW,
                capture.raw_reason or "no raw snapshot was taken",
            )
        requested += 1

        if capture.annotated_jpeg is not None:
            session.write(
                EvidenceKind.SNAPSHOT_ANNOTATED,
                SNAPSHOT_ANNOTATED,
                capture.annotated_jpeg,
                media_type=JPEG_MEDIA_TYPE,
            )
        else:
            session.fail(
                EvidenceKind.SNAPSHOT_ANNOTATED,
                SNAPSHOT_ANNOTATED,
                capture.annotated_reason or "no annotated snapshot was rendered",
            )
        requested += 1

        if self._config.clips_enabled:
            requested += self._write_clips(session, capture)
        session.write_json(EvidenceKind.TRACK_DUMP, TRACKS, self._tracks_payload(capture))
        session.write_json(EvidenceKind.SCENE_DUMP, SCENE, self._scene_payload(capture))
        session.write_json(
            EvidenceKind.EXPLANATION, EXPLANATION, self._explanation_payload(capture)
        )
        requested += 3

        report = session.finish(requested=requested)
        self._metrics.histogram("vigil_evidence_bytes_per_incident").observe(
            float(report.bytes_written)
        )
        self._log.info(
            "evidence_captured",
            incident_id=capture.incident_id,
            display_ref=capture.display_ref,
            status=report.status.value,
            items=len(report.records),
            bytes=report.bytes_written,
            failures=[f.reason for f in report.failures],
            notes=capture.notes,
        )
        return report

    def _write_clips(self, session: IncidentEvidence, capture: PendingCapture) -> int:
        buffer = self._buffers.get(capture.camera_id)
        if buffer is None:
            session.fail(EvidenceKind.CLIP_PRE, CLIP_PRE, "this camera has no evidence buffer")
            return 1
        preroll_ns = ms_to_ns(self._config.preroll_seconds * MS_PER_S)
        pre = buffer.window(
            since_ns=capture.first_observed_ns - preroll_ns, until_ns=capture.confirmed_ns
        )
        post = buffer.window(since_ns=capture.confirmed_ns + 1, until_ns=capture.deadline_ns)
        written = 0
        for kind, filename, frames in (
            (EvidenceKind.CLIP_PRE, CLIP_PRE, pre),
            (EvidenceKind.CLIP_POST, CLIP_POST, post),
        ):
            written += 1
            result = mux_clip(
                frames, session.directory / filename, anchor_ns=capture.first_observed_ns
            )
            if result.written:
                session.adopt(kind, filename, media_type=MP4_MEDIA_TYPE, span_ms=result.span_ms)
            else:
                session.fail(kind, filename, result.reason or "no clip was produced")
        return written

    # ---------------------------------------------------------------- payloads

    def _tracks_payload(self, capture: PendingCapture) -> object:
        scene = capture.scene
        if scene is None:
            return {"tracks": [], "note": "no scene was available at confirmation"}
        involved = capture.involved_track_ids
        return {
            "camera_id": capture.camera_id,
            "tracker_epoch": max((t.tracker_epoch for t in scene.tracks), default=0),
            "tracks": [
                {
                    "track_id": t.track_id,
                    "object_class": t.object_class.value,
                    "confidence_ratio": t.confidence_ratio,
                    "hits": t.hits,
                    "age_frames": t.age_frames,
                    "hit_ratio": round(t.hit_ratio, 4),
                    "flags": sorted(f.value for f in t.flags),
                    "velocity_px_s": (
                        None
                        if t.velocity_px_s is None
                        else {"x": t.velocity_px_s.x, "y": t.velocity_px_s.y}
                    ),
                    # px/s, and labelled as such: there is no ground plane, so m/s would be a
                    # number we cannot stand behind (04 §3.2).
                    "velocity_unit": "px/s",
                    "zone_dwell_ms": dict(t.zone_dwell_ms),
                    "trajectory": [
                        {
                            "t_monotonic_ns": p.t_monotonic_ns,
                            "bbox": [p.bbox.x1, p.bbox.y1, p.bbox.x2, p.bbox.y2],
                            "confidence_ratio": p.confidence_ratio,
                            "is_interpolated": p.is_interpolated,
                        }
                        for p in t.trajectory.points
                    ],
                }
                for t in scene.tracks
                if not involved or t.track_id in involved
            ],
        }

    def _scene_payload(self, capture: PendingCapture) -> object:
        scene = capture.scene
        if scene is None:
            return {"note": "no scene was available at confirmation"}
        meta = scene.frame_meta
        return {
            "frame": {
                "camera_id": meta.camera_id,
                "stream_epoch": meta.stream_epoch,
                "frame_index": meta.frame_index,
                "wall_utc": meta.wall_utc.isoformat(),
                "width_px": meta.width_px,
                "height_px": meta.height_px,
            },
            "stability_ratio": scene.stability_ratio,
            "capabilities": sorted(c.value for c in scene.capabilities),
            "degraded_facts": sorted(f.value for f in scene.degraded_facts),
            "zones": [
                {
                    "zone_id": z.zone_id,
                    "kind": z.kind.value,
                    "criticality": z.criticality,
                    "label": z.label,
                    "polygon": [[p.x, p.y] for p in z.polygon.points],
                    "active_at_frame": z.is_active_at(meta.wall_utc),
                }
                for z in scene.zones
            ],
            "occupancy": {
                zone_id: {
                    "track_ids": sorted(o.track_ids),
                    "counts_by_class": {k.value: v for k, v in o.counts_by_class.items()},
                    "density_per_kpx2": o.density_per_kpx2,
                    "entered": sorted(o.entered_track_ids),
                    "exited": sorted(o.exited_track_ids),
                }
                for zone_id, o in scene.occupancy.items()
            },
            "relations": [
                {
                    "kind": r.kind.value,
                    "subject_track_id": r.subject_track_id,
                    "object_track_id": r.object_track_id,
                    "zone_id": r.zone_id,
                    "distance_px": r.distance_px,
                }
                for r in scene.relations
            ],
        }

    def _explanation_payload(self, capture: PendingCapture) -> object:
        from vigil.persistence.mappers import explanation_to_json

        payload = explanation_to_json(capture.explanation)
        return {
            "incident_id": capture.incident_id,
            "display_ref": capture.display_ref,
            "detection_latency_ms": round(
                (capture.confirmed_ns - capture.first_observed_ns) / NS_PER_MS, 1
            ),
            "capture_notes": capture.notes,
            "explanation": payload,
        }
