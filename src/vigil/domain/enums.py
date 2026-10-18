"""Domain enumerations (05 §3).

`EventType` lists all ten requested categories because the enum is the taxonomy and should
be stable. Whether a category has a *module* is a runtime question answered by the
capability registry. The enum is not a promise of functionality; docs/LIMITATIONS.md says
which types have no implementation.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class ObjectClass(StrEnum):
    PERSON = "person"
    VEHICLE_CAR = "vehicle.car"
    VEHICLE_TRUCK = "vehicle.truck"
    VEHICLE_BUS = "vehicle.bus"
    VEHICLE_MOTORCYCLE = "vehicle.motorcycle"
    CYCLE_BICYCLE = "cycle.bicycle"
    OBJECT_BAG = "object.bag"
    OBJECT_BOX = "object.box"
    ANIMAL = "animal"
    FIRE = "fire"
    SMOKE = "smoke"
    UNKNOWN = "unknown"


class EventType(StrEnum):
    FIRE = "fire"
    SMOKE = "smoke"
    VEHICLE_COLLISION = "vehicle_collision"
    PERSON_DOWN = "person_down"
    CROWD_ANOMALY = "crowd_anomaly"
    INTRUSION = "intrusion"
    RESTRICTED_AREA_ENTRY = "restricted_area_entry"
    ABANDONED_OBJECT = "abandoned_object"
    DANGEROUS_MOTION = "dangerous_motion"
    VEHICLE_ANOMALY = "vehicle_anomaly"


class Severity(IntEnum):
    """Ordered: comparison is meaningful."""

    INFO = 0
    LOW = 1
    MODERATE = 2
    HIGH = 3
    CRITICAL = 4


class IncidentStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    ACTIVE = "active"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    EXPIRED = "expired"
    SUPPRESSED = "suppressed"


class CameraState(StrEnum):
    DISABLED = "disabled"
    INITIALIZING = "initializing"
    ONLINE = "online"
    DEGRADED = "degraded"
    ANALYSIS_SUSPENDED = "analysis_suspended"
    RECONNECTING = "reconnecting"
    OFFLINE = "offline"
    FAILED = "failed"


class SourceKind(StrEnum):
    WEBCAM = "webcam"
    RTSP = "rtsp"
    VIDEO_FILE = "video_file"
    IMAGE = "image"
    SYNTHETIC = "synthetic"


class ZoneKind(StrEnum):
    RESTRICTED = "restricted"
    EXCLUSION = "exclusion"
    COUNTING = "counting"
    CROSSING_LINE = "crossing_line"
    REGION_OF_INTEREST = "region_of_interest"


class EvidenceKind(StrEnum):
    SNAPSHOT_RAW = "snapshot_raw"
    SNAPSHOT_ANNOTATED = "snapshot_annotated"
    CLIP_PRE = "clip_pre"
    CLIP_POST = "clip_post"
    TRACK_DUMP = "track_dump"
    SCENE_DUMP = "scene_dump"
    EXPLANATION = "explanation"


class TrackFlag(StrEnum):
    """Lifecycle and motion state of a track (05 §4 lists these with an open-ended `...`).

    NEW           not yet confirmed: fewer than `tracking.min_hits` matched detections
    OCCLUDED      temporarily lost: no detection matched this update; the box is a prediction
    REACQUIRED    present for exactly the update in which a lost track was matched again
    STATIONARY    effectively not moving for at least `tracking.stationary_min_duration_ms`
    SUSPECT_MOTION  owned by P3's MotionSanityValidator; the tracker never sets it
    """

    NEW = "new"
    OCCLUDED = "occluded"
    REACQUIRED = "reacquired"
    STATIONARY = "stationary"
    SUSPECT_MOTION = "suspect_motion"


class SceneFact(StrEnum):
    """Facts a SceneState may fail to compute; listed in `degraded_facts` when missing."""

    ZONE_OCCUPANCY = "zone_occupancy"
    SPATIAL_RELATIONS = "spatial_relations"
    STABILITY = "stability"
    VELOCITY = "velocity"
    GROUND_PLANE = "ground_plane"


class SpatialRelationKind(StrEnum):
    NEAR = "near"
    INSIDE = "inside"
    APPROACHING = "approaching"
    CONVERGING = "converging"


class ValidatorOutcome(StrEnum):
    PASS = "pass"  # noqa: S105 - verdict label, not a credential
    REJECT = "reject"
    DEFER = "defer"


class CandidateOutcome(StrEnum):
    """What became of a promoted candidate (05 §5 `candidate_events.outcome`).

    Rejections are data, not waste: they are the only way to tune suppression without flying
    blind, and the fastest way to discover that a validator is silently eating real events
    (06 §6). Every outcome below except PENDING is terminal for that candidate.

    PENDING    promoted, verification not finished (a validator returned DEFER)
    VERIFIED   survived every validator; P4 turns this into an incident
    REJECTED   a validator objected; `rejection_reason` says which and why
    EXPIRED    held in DEFER past `verification.max_defer_ms` and never became decidable
    SUPPRESSED arbitration folded it into another event, which names it as a contributor
    """

    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPPRESSED = "suppressed"
