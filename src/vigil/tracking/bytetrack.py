"""ByteTrack-style tracker: detections in, persistent identities out (04 §3.2).

One instance per camera, touched by exactly one thread (D-005), so nothing here locks.

The association is two-stage, which is the whole idea behind ByteTrack and the reason it is
worth implementing rather than importing (03 §8): high-confidence detections are matched first
and are the only ones allowed to *start* a track, then the leftovers down to
`low_confidence_ratio` are offered to tracks that found no partner. A real object whose score
dips for a few frames — walking behind a railing, turning away from the light — keeps its
identity instead of dying and being reborn with a new id, while a weak detection on its own
still cannot conjure an object into existence.

Time is explicit everywhere. `dt` comes from frame timestamps, never an assumed frame rate,
because the scheduler grants inference at whatever rate it can afford and a filter that assumed
a fixed step would mis-predict every time that rate changed.

Two events invalidate every identity, and both bump `epoch`:

  * a **stream epoch change** — the camera reconnected, so the view may have changed and the
    frame indices restart;
  * a **gap longer than `reset_gap_ms`** — motion cannot be predicted across it, and silently
    reusing identities across the gap would fabricate trajectories.

A bumped epoch is how the rest of the system learns to discard everything keyed by a track id:
the event engine rebinds its modules, the accumulator store clears, and the suppression ledger
forgets the camera (04 §3.2, 06 §5 rule 4).

`zone_dwell_ms` is left empty here and filled in by the scene analyzer, which is the engine that
knows about zones. `tracking/` must not import `scene/` (01 §5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vigil.config.schema.modules import TrackingConfig
from vigil.core.geometry import BBox, Vector2
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import NS_PER_S, ns_to_ms
from vigil.domain.detection import Detection, DetectionSet
from vigil.domain.enums import ObjectClass, TrackFlag
from vigil.domain.frame import FrameMeta
from vigil.domain.track import TrackedObject, TrackPoint, Trajectory
from vigil.observability.metrics import MetricsRegistry
from vigil.tracking.assignment import greedy_match
from vigil.tracking.kalman import BoxKalman
from vigil.tracking.trajectory import fit_velocity_px_s, speed_box_heights_s


@dataclass(slots=True)
class _Track:
    """Mutable per-track state. Never leaves this module: `TrackedObject` is what callers see."""

    track_id: int
    kalman: BoxKalman
    class_votes: dict[ObjectClass, float]
    object_class: ObjectClass
    confidence_ratio: float
    first_seen_ns: int
    last_seen_ns: int  # the last update in which a detection was actually matched
    trajectory: Trajectory
    age_frames: int = 1
    hits: int = 1
    time_since_update_frames: int = 0
    matched_at_ns: int | None = None
    reacquired: bool = False
    stationary_since_ns: int | None = None
    velocity_px_s: Vector2 | None = None

    def vote(self, object_class: ObjectClass, weight: float, decay: float) -> None:
        """Decay every vote, then add this detection's, so one stray class cannot flip a track.

        Ties break on the class name rather than on dict order, so the winner is a function of
        the votes alone.
        """
        for key in list(self.class_votes):
            self.class_votes[key] *= decay
        self.class_votes[object_class] = self.class_votes.get(object_class, 0.0) + weight
        self.object_class = max(
            sorted(self.class_votes), key=lambda c: (self.class_votes[c], c.value)
        )


@dataclass(frozen=True, slots=True)
class _Stage:
    """What one association pass left over."""

    unmatched_tracks: tuple[_Track, ...]
    unmatched_detections: tuple[Detection, ...]


class ByteTracker:
    """Implements `core.protocols.tracker.Tracker`."""

    __slots__ = (
        "_camera_id",
        "_config",
        "_epoch",
        "_last_update_ns",
        "_log",
        "_metrics",
        "_next_track_id",
        "_stream_epoch",
        "_tracks",
    )

    def __init__(
        self,
        *,
        camera_id: str,
        config: TrackingConfig,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._camera_id = camera_id
        self._config = config
        self._log = logger.bind(camera_id=camera_id, stage="track")
        self._metrics = metrics
        self._tracks: list[_Track] = []
        self._next_track_id = 1
        self._epoch = 1
        self._last_update_ns: int | None = None
        self._stream_epoch: int | None = None

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def live_count(self) -> int:
        return len(self._tracks)

    def reset(self) -> None:
        """Forget every identity and bump the epoch.

        Track ids are never reused across epochs: `(camera_id, tracker_epoch, track_id)` is the
        identity (01 §7.7), and restarting ids at 1 would make two different objects
        indistinguishable in a log.
        """
        dropped = len(self._tracks)
        self._tracks.clear()
        self._epoch += 1
        self._last_update_ns = None
        self._metrics.counter("vigil_tracker_resets_total", camera=self._camera_id).inc()
        self._log.info("tracker_reset", epoch=self._epoch, dropped_tracks=dropped)

    # ---------------------------------------------------------------- the update

    def update(self, detections: DetectionSet, frame_meta: FrameMeta) -> Sequence[TrackedObject]:
        now_ns = frame_meta.t_monotonic_ns
        self._reset_if_discontinuous(frame_meta, now_ns)
        self._predict(now_ns)

        high, low = self._admit(detections)
        first = self._associate(
            tuple(self._tracks), high, now_ns, self._config.iou_match_threshold_ratio
        )
        self._associate(
            first.unmatched_tracks, low, now_ns, self._config.low_iou_match_threshold_ratio
        )
        self._mark_missed(now_ns)
        self._start_tracks(first.unmatched_detections, now_ns)
        self._retire(now_ns)
        self._enforce_capacity()

        if self._last_update_ns is None or now_ns > self._last_update_ns:
            self._last_update_ns = now_ns
        self._metrics.gauge("vigil_tracks_live", camera=self._camera_id).set(
            float(len(self._tracks))
        )
        ordered = sorted(self._tracks, key=lambda t: t.track_id)
        return tuple(self._emit(t, frame_meta) for t in ordered)

    def _predict(self, now_ns: int) -> None:
        dt_s = 0.0 if self._last_update_ns is None else (now_ns - self._last_update_ns) / NS_PER_S
        for track in self._tracks:
            track.kalman.predict(max(dt_s, 0.0))
            track.age_frames += 1
            track.matched_at_ns = None  # "matched in *this* update" starts false every update

    def _reset_if_discontinuous(self, frame_meta: FrameMeta, now_ns: int) -> None:
        if self._stream_epoch is None:
            self._stream_epoch = frame_meta.stream_epoch
        elif frame_meta.stream_epoch != self._stream_epoch:
            self._stream_epoch = frame_meta.stream_epoch
            self._log.info("tracker_reset_on_stream_epoch", stream_epoch=frame_meta.stream_epoch)
            self.reset()
            return
        if self._last_update_ns is not None:
            gap_ms = ns_to_ms(now_ns - self._last_update_ns)
            if gap_ms > self._config.reset_gap_ms:
                self._log.info("tracker_reset_on_gap", gap_ms=round(gap_ms, 1))
                self.reset()

    # ---------------------------------------------------------------- admission

    def _admit(
        self, detections: DetectionSet
    ) -> tuple[tuple[Detection, ...], tuple[Detection, ...]]:
        """Split usable detections into the high- and low-confidence bands.

        Degenerate and surplus detections are dropped and counted, never silently: a detector
        that suddenly produces three thousand boxes is a fact an operator needs, and an
        uncounted drop is indistinguishable from the tracker losing objects.
        """
        cfg = self._config
        usable: list[Detection] = []
        ignored = 0
        for d in detections.detections:
            degenerate = d.bbox.width < cfg.min_box_size_px or d.bbox.height < cfg.min_box_size_px
            if degenerate or d.confidence_ratio < cfg.low_confidence_ratio:
                ignored += 1
                continue
            usable.append(d)
        usable.sort(key=lambda d: -d.confidence_ratio)
        if len(usable) > cfg.max_detections_per_frame:
            ignored += len(usable) - cfg.max_detections_per_frame
            usable = usable[: cfg.max_detections_per_frame]
        if ignored:
            self._metrics.counter("vigil_detections_ignored_total", camera=self._camera_id).inc(
                ignored
            )
        high = tuple(d for d in usable if d.confidence_ratio >= cfg.high_confidence_ratio)
        low = tuple(d for d in usable if d.confidence_ratio < cfg.high_confidence_ratio)
        return (high, low)

    # ---------------------------------------------------------------- association

    def _associate(
        self,
        tracks: Sequence[_Track],
        candidates: Sequence[Detection],
        now_ns: int,
        min_iou: float,
    ) -> _Stage:
        if not tracks or not candidates:
            return _Stage(tuple(tracks), tuple(candidates))
        penalty = self._config.class_mismatch_penalty_ratio
        similarity = [
            [
                t.kalman.box.iou(d.bbox)
                - (penalty if d.object_class is not t.object_class else 0.0)
                for d in candidates
            ]
            for t in tracks
        ]
        result = greedy_match(
            similarity, min_similarity=min_iou, rows=len(tracks), columns=len(candidates)
        )
        for row, column in result.matches:
            self._match(tracks[row], candidates[column], now_ns)
        return _Stage(
            unmatched_tracks=tuple(tracks[r] for r in result.unmatched_rows),
            unmatched_detections=tuple(candidates[c] for c in result.unmatched_columns),
        )

    def _match(self, track: _Track, detection: Detection, now_ns: int) -> None:
        track.reacquired = track.time_since_update_frames > 0
        track.kalman.update(detection.bbox)
        track.hits += 1
        track.time_since_update_frames = 0
        track.matched_at_ns = now_ns
        track.last_seen_ns = max(track.last_seen_ns, now_ns)
        track.confidence_ratio = detection.confidence_ratio
        track.vote(
            detection.object_class,
            detection.confidence_ratio,
            self._config.class_vote_decay_ratio,
        )
        self._append_point(track, now_ns, detection.bbox, detection.confidence_ratio, False)
        self._refresh_motion(track)

    def _mark_missed(self, now_ns: int) -> None:
        """A track nothing matched this update is predicted forward, and says so.

        The predicted box is recorded as an interpolated trajectory point so that velocity —
        which is fitted over measured points only — cannot be computed from the filter's own
        extrapolation (05 §4).
        """
        for track in self._tracks:
            if track.matched_at_ns is not None:
                continue
            track.time_since_update_frames += 1
            track.reacquired = False
            self._append_point(track, now_ns, track.kalman.box, track.confidence_ratio, True)

    def _append_point(
        self, track: _Track, t_ns: int, box: BBox, confidence: float, interpolated: bool
    ) -> None:
        last = track.trajectory.points[-1] if len(track.trajectory) else None
        if last is not None and t_ns < last.t_monotonic_ns:
            return  # a Trajectory stays time-ordered; an out-of-order frame adds nothing
        track.trajectory = track.trajectory.with_point(
            TrackPoint(
                t_monotonic_ns=t_ns,
                bbox=box,
                confidence_ratio=confidence,
                is_interpolated=interpolated,
            )
        )

    def _refresh_motion(self, track: _Track) -> None:
        """Recompute the reported velocity and maintain the stationary stretch.

        `stationary_since_ns` survives the bounded trajectory window on purpose: an abandoned
        object may have been still for minutes, which no 90-point history could show.
        """
        cfg = self._config
        track.velocity_px_s = fit_velocity_px_s(
            track.trajectory.points,
            window_frames=cfg.velocity_window_frames,
            min_points=cfg.min_history_for_velocity,
        )
        speed = speed_box_heights_s(track.velocity_px_s, track.kalman.box.height)
        if speed is None or speed >= cfg.stationary_speed_ratio:
            track.stationary_since_ns = None
        elif track.stationary_since_ns is None:
            track.stationary_since_ns = track.last_seen_ns

    # ---------------------------------------------------------------- lifecycle

    def _start_tracks(self, unmatched_high: Sequence[Detection], now_ns: int) -> None:
        """Only a high-confidence detection may start a track. A weak detection on its own is
        not evidence that an object exists; it is only evidence about one that already did."""
        cfg = self._config
        for detection in unmatched_high:
            if len(self._tracks) >= cfg.max_tracks and not self._evict_one():
                return
            track = _Track(
                track_id=self._next_track_id,
                kalman=BoxKalman(
                    detection.bbox,
                    acceleration_noise_ratio=cfg.kalman_acceleration_noise_ratio,
                    measurement_noise_ratio=cfg.kalman_measurement_noise_ratio,
                    min_size_px=cfg.min_box_size_px,
                ),
                class_votes={detection.object_class: detection.confidence_ratio},
                object_class=detection.object_class,
                confidence_ratio=detection.confidence_ratio,
                first_seen_ns=now_ns,
                last_seen_ns=now_ns,
                trajectory=Trajectory((), cfg.history_length),
                matched_at_ns=now_ns,
            )
            self._append_point(track, now_ns, detection.bbox, detection.confidence_ratio, False)
            self._next_track_id += 1
            self._tracks.append(track)
            self._metrics.counter("vigil_tracks_created_total", camera=self._camera_id).inc()

    def _retire(self, now_ns: int) -> None:
        cfg = self._config
        kept: list[_Track] = []
        for track in self._tracks:
            if track.time_since_update_frames == 0:
                kept.append(track)
                continue
            tentative = track.hits < cfg.min_hits
            lost_ms = ns_to_ms(now_ns - track.last_seen_ns)
            expired = (
                track.time_since_update_frames > cfg.max_age_frames
                or lost_ms > cfg.max_lost_ms
                or (tentative and track.time_since_update_frames > cfg.tentative_max_age_frames)
            )
            if expired:
                self._metrics.counter("vigil_tracks_terminated_total", camera=self._camera_id).inc()
                continue
            kept.append(track)
        self._tracks = kept

    def _evict_one(self) -> bool:
        """Make room by dropping the least valuable live track.

        Least valuable means fewest hits, then longest unmatched, then oldest id — never the
        most recently seen object, which is the one most likely to matter.
        """
        if not self._tracks:
            return False
        victim = min(self._tracks, key=lambda t: (t.hits, -t.time_since_update_frames, -t.track_id))
        self._tracks.remove(victim)
        self._metrics.counter("vigil_tracks_evicted_total", camera=self._camera_id).inc()
        self._log.warning(
            "track_evicted_at_capacity",
            track_id=victim.track_id,
            max_tracks=self._config.max_tracks,
        )
        return True

    def _enforce_capacity(self) -> None:
        while len(self._tracks) > self._config.max_tracks and self._evict_one():
            pass

    # ---------------------------------------------------------------- output

    def _flags(self, track: _Track) -> frozenset[TrackFlag]:
        flags: set[TrackFlag] = set()
        if track.hits < self._config.min_hits:
            flags.add(TrackFlag.NEW)
        if track.time_since_update_frames > 0:
            flags.add(TrackFlag.OCCLUDED)
        if track.reacquired:
            flags.add(TrackFlag.REACQUIRED)
        if track.stationary_since_ns is not None:
            held_ms = ns_to_ms(track.last_seen_ns - track.stationary_since_ns)
            if held_ms >= self._config.stationary_min_duration_ms:
                flags.add(TrackFlag.STATIONARY)
        return frozenset(flags)

    def _emit(self, track: _Track, frame_meta: FrameMeta) -> TrackedObject:
        flags = self._flags(track)
        return TrackedObject(
            track_id=track.track_id,
            camera_id=self._camera_id,
            tracker_epoch=self._epoch,
            object_class=track.object_class,
            bbox=track.kalman.box.clipped(float(frame_meta.width_px), float(frame_meta.height_px)),
            confidence_ratio=track.confidence_ratio,
            first_seen_monotonic_ns=track.first_seen_ns,
            last_seen_monotonic_ns=track.last_seen_ns,
            age_frames=track.age_frames,
            hits=track.hits,
            time_since_update_frames=track.time_since_update_frames,
            trajectory=track.trajectory,
            velocity_px_s=track.velocity_px_s,
            velocity_m_s=None,  # needs GROUND_PLANE; px/s is the honest unit until then
            zone_dwell_ms={},  # filled by the scene analyzer, which is what knows about zones
            flags=flags,
            stationary_since_monotonic_ns=(
                track.stationary_since_ns if TrackFlag.STATIONARY in flags else None
            ),
        )
