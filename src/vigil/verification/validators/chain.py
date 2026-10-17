"""The eleven validators of 06 §6, in order.

Order is cheapest-and-most-decisive first: health and stability are single comparisons that
kill whole classes of candidate, so they run before anything that walks a track's trajectory.

Every validator returns a verdict with a mandatory human-readable reason, including on PASS.
"Which validators ran and said yes" is as diagnostically valuable as the rejections, and without
it you cannot tell a validator that passed from one that never executed.

None of these reads a clock. `state.now_monotonic_ns` comes from the frame.
"""

from __future__ import annotations

from vigil.core.capabilities import Availability
from vigil.core.units import ns_to_ms
from vigil.domain.camera import CameraHealth
from vigil.domain.enums import CameraState, EventType, SceneFact, TrackFlag, ZoneKind
from vigil.domain.event import CandidateEvent, ValidatorVerdict
from vigil.domain.scene import SceneState
from vigil.domain.track import TrackedObject
from vigil.verification.ledger import dedup_key
from vigil.verification.validators.context import (
    VerificationState,
    deferred,
    passed,
    rejected,
    subject_track_id,
)

UNHEALTHY_STATES = frozenset(
    {
        CameraState.RECONNECTING,
        CameraState.DEGRADED,
        CameraState.OFFLINE,
        CameraState.FAILED,
        CameraState.ANALYSIS_SUSPENDED,
    }
)
# Structural requirements a candidate's own shape must satisfy, per event type (validator 6).
MIN_TRACKS_BY_TYPE: dict[EventType, int] = {
    EventType.VEHICLE_COLLISION: 2,
    EventType.CROWD_ANOMALY: 3,
}


def _track(scene: SceneState | None, candidate: CandidateEvent) -> TrackedObject | None:
    track_id = subject_track_id(candidate.subject)
    if scene is None or track_id is None:
        return None
    try:
        return scene.track_by_id(track_id)
    except KeyError:
        return None


# ------------------------------------------------------------------ 1. camera health


def validate_camera_health(candidate: CandidateEvent, state: VerificationState) -> ValidatorVerdict:
    """Detections from a stuttering stream have corrupted temporal spacing, so any judgement
    built on them is unsound — however convincing the detections themselves look."""
    vid = "camera_health"
    health: CameraHealth | None = state.camera_health
    if health is None:
        return passed(vid, "no camera health available to object with")
    if health.state in UNHEALTHY_STATES:
        return rejected(vid, f"camera is {health.state.value}: {health.detail or 'unhealthy'}")
    if state.scene is not None:
        age_ms = ns_to_ms(state.now_monotonic_ns - state.scene.frame_meta.t_monotonic_ns)
        if age_ms > state.config.max_frame_age_ms:
            return rejected(
                vid,
                f"frame is {age_ms:.0f} ms old, beyond the "
                f"{state.config.max_frame_age_ms:.0f} ms limit",
            )
    return passed(vid, f"camera is {health.state.value}")


# ------------------------------------------------------------------ 2. scene stability


def validate_scene_stability(
    candidate: CandidateEvent, state: VerificationState
) -> ValidatorVerdict:
    """Lights switching on, auto-exposure hunting, a bumped camera. In practice the largest
    single source of correlated false positives."""
    vid = "scene_stability"
    scene = state.scene
    if scene is None:
        return passed(vid, "no scene available to object with")
    if SceneFact.STABILITY in scene.degraded_facts:
        return passed(vid, "stability could not be computed; not treated as instability")
    if scene.stability_ratio <= state.config.min_stability_ratio:
        return rejected(
            vid,
            f"scene stability {scene.stability_ratio:.2f} at or below "
            f"{state.config.min_stability_ratio:.2f}: the view is changing, not the subject",
        )
    since = state.instability_since_monotonic_ns.get(candidate.camera_id)
    if since is not None:
        elapsed_ms = ns_to_ms(state.now_monotonic_ns - since)
        if elapsed_ms < state.config.instability_blackout_ms:
            return rejected(
                vid,
                f"within the {state.config.instability_blackout_ms} ms blackout after "
                f"instability ({elapsed_ms:.0f} ms ago)",
            )
    return passed(vid, f"scene stability {scene.stability_ratio:.2f}")


# ------------------------------------------------------------------ 3. track quality


def validate_track_quality(candidate: CandidateEvent, state: VerificationState) -> ValidatorVerdict:
    """Mostly-predicted tracks are hallucinated motion."""
    vid = "track_quality"
    if candidate.subject.kind != "track":
        return passed(vid, f"subject {candidate.subject} is not a track")
    track = _track(state.scene, candidate)
    if track is None:
        return rejected(vid, f"subject {candidate.subject} is not present in the scene")
    if track.hit_ratio < state.config.min_track_hit_ratio:
        return rejected(
            vid,
            f"hit ratio {track.hit_ratio:.2f} below {state.config.min_track_hit_ratio:.2f} "
            f"({track.hits} hits in {track.age_frames} frames): mostly prediction",
        )
    age_ms = ns_to_ms(track.last_seen_monotonic_ns - track.first_seen_monotonic_ns)
    if age_ms < state.config.min_track_age_ms:
        return deferred(
            vid,
            f"track is {age_ms:.0f} ms old, under {state.config.min_track_age_ms:.0f} ms: "
            "needs more history before it can be judged",
        )
    return passed(vid, f"hit ratio {track.hit_ratio:.2f} over {age_ms:.0f} ms")


# ------------------------------------------------------------------ 4. motion sanity


def validate_motion_sanity(candidate: CandidateEvent, state: VerificationState) -> ValidatorVerdict:
    """A person at 200 px/s across a 640 px frame is an ID switch, not a sprinter.

    The bound is in box heights per second rather than pixels, so it means the same thing for a
    near subject and a far one.
    """
    vid = "motion_sanity"
    track = _track(state.scene, candidate)
    if track is None:
        return passed(vid, "no track to check")
    if TrackFlag.SUSPECT_MOTION in track.flags:
        return rejected(vid, "track is already flagged suspect_motion")
    speed = track.speed_px_s
    if speed is None:
        return passed(vid, "velocity unknown; nothing to object to")
    height = max(track.bbox.y2 - track.bbox.y1, 1.0)
    relative = speed / height
    if relative > state.config.max_speed_box_heights_s:
        return rejected(
            vid,
            f"implied speed {relative:.1f} box heights/s exceeds "
            f"{state.config.max_speed_box_heights_s:.1f}: an identity switch, not motion",
        )
    return passed(vid, f"implied speed {relative:.1f} box heights/s is plausible")


# ------------------------------------------------------------------ 5. zone gate


def validate_zone_gate(candidate: CandidateEvent, state: VerificationState) -> ValidatorVerdict:
    """Spatial and temporal masking of known-noisy areas.

    The most effective false-positive suppression in practice is spatial: mask off the road
    visible through the fence, the TV in the corner, the reflective window.
    """
    vid = "zone_gate"
    scene = state.scene
    if scene is None:
        return passed(vid, "no scene available to object with")
    track = _track(scene, candidate)
    if track is not None:
        for zone in scene.zones:
            if zone.kind is not ZoneKind.EXCLUSION:
                continue
            occupancy = scene.occupancy.get(zone.zone_id)
            if occupancy is not None and track.track_id in occupancy.track_ids:
                return rejected(vid, f"subject is inside exclusion zone {zone.zone_id!r}")
    governing = [z for z in scene.zones if z.zone_id in candidate.zone_ids]
    inactive = [z for z in governing if not z.is_active_at(scene.frame_meta.wall_utc)]
    if governing and len(inactive) == len(governing):
        names = ", ".join(sorted(z.zone_id for z in inactive))
        return rejected(vid, f"every governing zone is outside its schedule: {names}")
    return passed(vid, f"{len(governing)} governing zone(s) active, no exclusion hit")


# ------------------------------------------------------------------ 6. co-requirement


def validate_co_requirement(
    candidate: CandidateEvent, state: VerificationState
) -> ValidatorVerdict:
    """Catches a module that fired on a structurally impossible configuration."""
    vid = "co_requirement"
    needed = MIN_TRACKS_BY_TYPE.get(candidate.event_type)
    if needed is None:
        return passed(vid, f"{candidate.event_type.value} has no structural requirement")
    have = len(candidate.involved_track_ids)
    if have < needed:
        return rejected(
            vid,
            f"{candidate.event_type.value} needs at least {needed} involved tracks, got {have}",
        )
    return passed(vid, f"{have} involved tracks meets the minimum of {needed}")


# ------------------------------------------------------------------ 7. duplicate


def validate_duplicate(candidate: CandidateEvent, state: VerificationState) -> ValidatorVerdict:
    """Prevents a second event for the same ongoing situation; instead it **extends** the open
    one. The dedup key is the identity of a situation, not of a frame."""
    vid = "duplicate"
    key = dedup_key(candidate.camera_id, candidate.event_type, candidate.subject)
    open_event = state.ledger.open_event(key)
    if open_event is None:
        return passed(vid, "no open event for this camera, type and subject")
    return rejected(
        vid,
        f"event {open_event.candidate_id} is already open for this situation "
        f"({open_event.duration_ms(state.now_monotonic_ns):.0f} ms); extending it instead",
    )


# ------------------------------------------------------------------ 8. cooldown


def validate_cooldown(candidate: CandidateEvent, state: VerificationState) -> ValidatorVerdict:
    """Stops resolve/refire oscillation at a boundary."""
    vid = "cooldown"
    key = dedup_key(candidate.camera_id, candidate.event_type, candidate.subject)
    if state.ledger.in_cooldown(key, state.now_monotonic_ns):
        remaining = state.ledger.cooldown_remaining_ms(key, state.now_monotonic_ns)
        return rejected(
            vid,
            f"an event for this situation resolved under {state.config.cooldown_ms} ms ago; "
            f"{remaining:.0f} ms of cooldown remain",
        )
    return passed(vid, "no recent resolution for this situation")


# ------------------------------------------------------------------ 9. rate limit


def validate_rate_limit(candidate: CandidateEvent, state: VerificationState) -> ValidatorVerdict:
    """An alert storm is worse than a missed alert: it destroys the operator's ability to see
    anything."""
    vid = "rate_limit"
    count = state.ledger.recent_count(candidate.camera_id, state.now_monotonic_ns)
    limit = state.config.max_events_per_window
    if count >= limit:
        return rejected(
            vid,
            f"camera {candidate.camera_id} already opened {count} events in the last "
            f"{state.config.rate_limit_window_ms / 1000:.0f} s, at the limit of {limit}",
        )
    return passed(vid, f"{count} of {limit} events used in the window")


# ------------------------------------------------------------------ 10. operator suppression


def validate_operator_suppression(
    candidate: CandidateEvent, state: VerificationState
) -> ValidatorVerdict:
    """Honours "I know, the contractors are working there until 5"."""
    vid = "operator_suppression"
    for mute in state.mutes:
        if mute.covers(
            camera_id=candidate.camera_id,
            event_type=candidate.event_type,
            zone_ids=candidate.zone_ids,
            now_ns=state.now_monotonic_ns,
        ):
            return rejected(vid, f"suppressed by an operator mute: {mute.reason}")
    return passed(vid, "no operator mute covers this event")


# ------------------------------------------------------------------ 11. capability caveat


def validate_capability_caveat(
    candidate: CandidateEvent, state: VerificationState
) -> ValidatorVerdict:
    """Never rejects. Annotates the event so a DEGRADED module's limitation travels with it."""
    vid = "capability_caveat"
    if state.module_availability is Availability.DEGRADED:
        missing = ", ".join(state.missing_optional) or "an optional capability"
        return passed(vid, f"module is DEGRADED: running without {missing}")
    return passed(vid, "module is running with every capability it declared")


DEFAULT_CHAIN = (
    validate_camera_health,
    validate_scene_stability,
    validate_track_quality,
    validate_motion_sanity,
    validate_zone_gate,
    validate_co_requirement,
    validate_duplicate,
    validate_cooldown,
    validate_rate_limit,
    validate_operator_suppression,
    validate_capability_caveat,
)
