"""Tracking, scene, temporal, verification and module settings (04 §6, 06).

Every threshold has one validated home here, so no magic number lives in source. Names carry
their unit (`_ms`, `_px`, `_ratio`, `_frames`). A spatial threshold is expressed relative to
the object's own box height rather than in pixels, because a pixel value silently means
different things at 640x480 and at 1920x1080; the field comment says what each ratio is
relative to. Cross-field invariants are enforced here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel

ZoneAnchor = Literal["bottom_center", "center"]


class TrackingConfig(ConfigModel):
    """The tracking engine (04 §3.2): ByteTrack-style association over a Kalman motion model.

    "Frames" in this section are tracker updates, i.e. frames that were inferred. Capture frames
    the scheduler skipped never reach the tracker, so these counts do not depend on the capture
    rate.
    """

    enabled: bool = True

    # --- lifecycle
    # A track that has gone unmatched for more than this many updates, or this long, is
    # terminated. The time bound keeps a slow inference rate from stretching the frame bound.
    max_age_frames: int = Field(default=30, ge=1)
    max_lost_ms: int = Field(default=10_000, ge=1)
    # Matched detections needed before a track stops being flagged NEW.
    min_hits: int = Field(default=3, ge=1)
    # An unconfirmed track is terminated sooner: one stray detection must not live as long as
    # a real object. It survives this many consecutive missed updates.
    tentative_max_age_frames: int = Field(default=1, ge=0)
    # A gap between two updates longer than this resets the tracker: motion cannot be predicted
    # across it, and silently reusing identities would fabricate trajectories (04 §3.2).
    reset_gap_ms: int = Field(default=5000, ge=1)
    # Hard bound on live tracks per camera. When reached, the least valuable track is evicted.
    max_tracks: int = Field(default=200, ge=1)

    # --- association
    # Detections at or above `high_confidence_ratio` are matched first and may start tracks;
    # detections between `low_confidence_ratio` and it are only used to keep an existing track
    # alive (this is what lets a track survive a confidence dip); lower ones are ignored.
    high_confidence_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    low_confidence_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    iou_match_threshold_ratio: float = Field(default=0.3, ge=0.0, le=1.0)
    low_iou_match_threshold_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    # Added to the association cost when a detection's class differs from the track's, so a
    # briefly misclassified object stays one track but a different object does not steal it.
    class_mismatch_penalty_ratio: float = Field(default=0.2, ge=0.0, le=1.0)
    # Per-update decay of a track's class votes. A single stray class cannot flip a track.
    class_vote_decay_ratio: float = Field(default=0.9, ge=0.0, le=1.0)
    # Detections narrower or shorter than this are degenerate and ignored.
    min_box_size_px: float = Field(default=2.0, gt=0.0)

    # Bounds the per-update work however noisy the detector is; the lowest-confidence
    # detections beyond it are ignored and counted.
    max_detections_per_frame: int = Field(default=300, ge=1)

    # --- motion model: constant velocity with random acceleration. Both noises are fractions
    # of the box's own size, so they scale with the object (and with its distance).
    # Acceleration: standard deviation in box heights per second squared. Larger trusts new
    # measurements over the motion prediction. Measurement: detector localisation noise.
    kalman_acceleration_noise_ratio: float = Field(default=2.0, gt=0.0)
    kalman_measurement_noise_ratio: float = Field(default=0.05, gt=0.0)

    # --- trajectory and motion
    history_length: int = Field(default=90, ge=2)
    # Velocity is None until this many matched points exist, then a least-squares slope over
    # the most recent `velocity_window_frames` of them (pixels per second; never metres).
    min_history_for_velocity: int = Field(default=5, ge=2)
    velocity_window_frames: int = Field(default=10, ge=2)
    # A track is STATIONARY once its speed, in box heights per second, has stayed below
    # `stationary_speed_ratio` for `stationary_min_duration_ms`. Relative to the box so the
    # threshold means the same thing for a near object and a far one.
    stationary_speed_ratio: float = Field(default=0.1, ge=0.0)
    stationary_min_duration_ms: int = Field(default=1000, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.min_history_for_velocity > self.history_length:
            raise ValueError("min_history_for_velocity cannot exceed history_length")
        if not self.min_history_for_velocity <= self.velocity_window_frames <= self.history_length:
            raise ValueError(
                "velocity_window_frames must lie between min_history_for_velocity and "
                "history_length"
            )
        if self.low_confidence_ratio >= self.high_confidence_ratio:
            raise ValueError("low_confidence_ratio must be below high_confidence_ratio")
        return self


class SceneConfig(ConfigModel):
    """Scene analysis (04 §3.3): zones, occupancy, spatial relations, stability."""

    region_grid: int = Field(default=4, ge=1, le=32)  # used by region subjects (P3+)

    # --- zones
    # Which point of a box decides whether an object is "in" a zone. A person stands on the
    # ground, so the foot point (bottom centre) is right for anything that rests on the floor;
    # the centre is right for things with no footprint, such as flames.
    zone_anchor: ZoneAnchor = "bottom_center"
    center_anchor_classes: tuple[str, ...] = ("fire", "smoke")
    max_zones_per_camera: int = Field(default=64, ge=1)

    # --- spatial relations (only between confirmed, currently visible tracks)
    # NEAR: the gap between two boxes, in mean box heights, is at most this.
    near_distance_ratio: float = Field(default=0.5, ge=0.0)
    # APPROACHING a zone: outside it, within this many box heights of its edge, and closing on
    # it faster than `approach_min_closing_speed_ratio` box heights per second.
    approach_max_distance_ratio: float = Field(default=3.0, gt=0.0)
    approach_min_closing_speed_ratio: float = Field(default=0.1, ge=0.0)
    # Pairwise relations are quadratic in the track count. Above this many eligible tracks they
    # are skipped and SPATIAL_RELATIONS is reported as a degraded fact rather than computed late.
    max_relation_tracks: int = Field(default=64, ge=2)

    # --- stability: the whole-frame brightness/structure change between analysed frames
    stability_enabled: bool = True
    stability_grid_columns: int = Field(default=16, ge=2)
    stability_grid_rows: int = Field(default=12, ge=2)
    # Median per-cell luminance change (fraction of full scale) at which stability reads 0.
    # The median is robust to a person walking through the frame; a lights-on step moves every
    # cell and a bumped camera moves most of them.
    stability_full_change_ratio: float = Field(default=0.1, gt=0.0, le=1.0)


class TemporalConfig(ConfigModel):
    """Log-odds accumulation with half-life decay and dual-threshold hysteresis (D-007, 04 §6).

    The update rule is specified exactly in 04 §6.2 and implemented in `temporal/accumulator.py`.
    These defaults are starting points for tuning against recorded footage, not tuned values.
    """

    half_life_ms: float = Field(default=2000.0, gt=0)
    signal_floor: float = Field(default=0.02, gt=0.0, lt=0.5)
    l_bound: float = Field(default=8.0, gt=0)
    l_activate: float = Field(default=2.2, gt=0)
    l_release: float = Field(default=0.8, ge=0)
    confidence_scale: float = Field(default=1.0, gt=0)
    min_observations: int = Field(default=4, ge=1)
    min_support_ratio: float = Field(default=0.6, ge=0.0, le=1.0)
    min_duration_ms: int = Field(default=1200, ge=0)
    release_sustain_ms: int = Field(default=2000, ge=0)

    # How much of the support window one observation is expected to cover. `support_ratio` is
    # the fraction of slots this wide that carried supporting evidence, which is what makes
    # four strong frames scattered through fifteen seconds fail the test while twelve
    # consecutive frames pass it (04 §6.3). None derives it from the camera's
    # `target_inference_fps`, which is the rate observations actually arrive at.
    support_slot_ms: float | None = Field(default=None, gt=0)
    # Support is measured over at most this much of the recent past, so a long-running
    # accumulator's ratio reflects the current situation rather than its whole history.
    support_window_ms: float = Field(default=10_000.0, gt=0)
    # Hard bound on retained observations per accumulator. Reached only by a very long window
    # at a high frame rate; the oldest are dropped first and counted.
    max_window_observations: int = Field(default=512, ge=2)

    # An inactive accumulator with no observation for this long is discarded: its evidence has
    # decayed to irrelevance and keeping it would leak memory, one subject at a time.
    idle_expiry_ms: float = Field(default=30_000.0, gt=0)

    # Evidence weight per module, used in step 3 of the update rule. A module whose signal is
    # known to be noisy can be trusted less without changing its code.
    default_module_weight: float = Field(default=1.0, gt=0)
    module_weights: Mapping[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _hysteresis(self) -> Self:
        if self.l_release >= self.l_activate:
            raise ValueError(
                "l_release must be below l_activate: the gap is the hysteresis that stops "
                "an incident flapping (04 §6.3)"
            )
        if self.l_activate > self.l_bound:
            raise ValueError("l_activate cannot exceed l_bound, or nothing could ever confirm")
        if any(w <= 0.0 for w in self.module_weights.values()):
            raise ValueError("every module weight must be positive")
        return self

    def weight_for(self, module_id: str) -> float:
        return self.module_weights.get(module_id, self.default_module_weight)


class VerificationConfig(ConfigModel):
    """The validator chain (06 §6). Fails closed: unverifiable means rejected."""

    max_defer_ms: int = Field(default=10_000, ge=0)
    cooldown_ms: int = Field(default=60_000, ge=0)
    instability_blackout_ms: int = Field(default=3000, ge=0)
    min_track_hit_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    # A track younger than this has too little history to judge; validator 3 DEFERs rather than
    # rejecting, because the answer may simply not be knowable yet.
    min_track_age_ms: float = Field(default=400.0, ge=0.0)
    # Validator 1: a frame older than this has corrupted temporal spacing, so any judgement
    # built on it is unsound however good the detections look.
    max_frame_age_ms: float = Field(default=2000.0, gt=0.0)
    # Validator 2: stability at or below this reads as a scene change (lights, a camera bump).
    min_stability_ratio: float = Field(default=0.6, ge=0.0, le=1.0)
    # Validator 9: an alert storm destroys the operator's ability to see anything, so it is
    # worse than a missed alert. Counted per camera over the window.
    max_events_per_window: int = Field(default=12, ge=1)
    rate_limit_window_ms: float = Field(default=600_000.0, gt=0.0)
    # Validator 4: implied speed above this, in box heights per second, is an identity switch
    # rather than a sprinter. Relative to the box so it means the same near and far.
    max_speed_box_heights_s: float = Field(default=6.0, gt=0.0)
    # How much a DEGRADED module's confidence is reduced. Not zero, because a caveated event is
    # still worth showing; not large, because the module still did its job.
    degraded_confidence_penalty_ratio: float = Field(default=0.1, ge=0.0, le=0.5)


class ArbitrationConfig(ConfigModel):
    """Relationships between events that no single-candidate validator can see (06 §7).

    A small, explicit rule set an operator can read, deliberately not a learned layer.
    """

    # Two events of one type on one camera inside this window are related; the count feeds the
    # severity engine's `escalation` factor.
    escalation_window_ms: float = Field(default=120_000.0, gt=0.0)
    escalation_min_events: int = Field(default=3, ge=2)
    # Reserved for the merge rule, which needs FIRE/SMOKE (P6). Declared here so the key exists
    # where the architecture says it does, and documented as unimplemented in LIMITATIONS.md.
    merge_window_ms: float = Field(default=10_000.0, gt=0.0)


class ModulesConfig(ConfigModel):
    """`enabled` is an explicit allowlist. Discovering a module never enables it."""

    observe_budget_ms: float = Field(default=5.0, gt=0)
    quarantine_threshold: int = Field(default=3, ge=1)
    quarantine_retry_ms: int = Field(default=30_000, ge=0)
    enabled: tuple[str, ...] = ()
