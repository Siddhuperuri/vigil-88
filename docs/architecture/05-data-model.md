# 05 — Data Model

> Status: DESIGN. Types below are the contracts P0 will create in `src/vigil/domain/`.

## 1. Rules

1. **Frozen by default.** Every domain type is `@dataclass(frozen=True, slots=True)`.
   Objects cross thread boundaries (01 §8); immutability makes that safe without locks.
   `slots=True` matters because `Detection` and `TrackPoint` are allocated by the million.
2. **No I/O, no third-party imports.** `domain/` imports only the stdlib and `core/`
   (02 §5). It is importable in a bare interpreter with no torch, no cv2, no pydantic.
3. **Units in names.** `duration_ms`, `velocity_px_s`, `area_px2`, `vram_mb`,
   `confidence_ratio`. A bare `timeout` or `size` is a defect.
4. **Timestamps come in pairs.** `t_monotonic_ns` for all arithmetic and ordering,
   `wall_utc` for display and persistence. Monotonic clocks do not jump when NTP corrects
   or when the laptop wakes from sleep; wall clocks are what an operator reads. Mixing them
   up is the classic bug in this class of system.
5. **Optional means optional.** No sentinel values. No `-1` for unknown, no `0.0` for
   missing, no empty string for absent. `None` with a typed `| None`.

## 2. Type map

```
  Camera ---- CameraHealth
    |
    v
  Frame (FrameMeta + PixelBuffer)
    |
    v
  DetectionSet -- Detection*
    |
    v
  TrackedObject* -- Trajectory -- TrackPoint*
    |
    v
  SceneState -- ZoneOccupancy* -- Zone*
    |         \- SpatialRelation*
    v
  Observation*        (per module, per subject, per frame)
    |
    v
  AccumulatorState    (per camera+module+subject)
    |
    v
  CandidateEvent
    |
    v
  VerifiedEvent -- ValidatorVerdict*
    |
    v
  Incident -- IncidentTransition*
    |      \- IncidentExplanation
    |      \- Evidence*
    v
  Alert -- AlertDelivery*

  SystemMetric, StageLatency   (orthogonal; keyed by time)
```

`*` = collection.

## 3. Enumerations (`domain/enums.py`)

```python
class ObjectClass(StrEnum):
    PERSON = "person"
    VEHICLE_CAR = "vehicle.car";      VEHICLE_TRUCK = "vehicle.truck"
    VEHICLE_BUS = "vehicle.bus";      VEHICLE_MOTORCYCLE = "vehicle.motorcycle"
    CYCLE_BICYCLE = "cycle.bicycle"
    OBJECT_BAG = "object.bag";        OBJECT_BOX = "object.box"
    ANIMAL = "animal"
    FIRE = "fire";                    SMOKE = "smoke"
    UNKNOWN = "unknown"

class EventType(StrEnum):               # the ten categories; most are not implemented yet
    FIRE = "fire";                      SMOKE = "smoke"
    VEHICLE_COLLISION = "vehicle_collision"
    PERSON_DOWN = "person_down"
    CROWD_ANOMALY = "crowd_anomaly"
    INTRUSION = "intrusion"
    RESTRICTED_AREA_ENTRY = "restricted_area_entry"
    ABANDONED_OBJECT = "abandoned_object"
    DANGEROUS_MOTION = "dangerous_motion"
    VEHICLE_ANOMALY = "vehicle_anomaly"

class Severity(IntEnum):                # ordered: comparison is meaningful
    INFO = 0; LOW = 1; MODERATE = 2; HIGH = 3; CRITICAL = 4

class IncidentStatus(StrEnum):
    CANDIDATE = "candidate";   CONFIRMED = "confirmed";  ACTIVE = "active"
    RESOLVING = "resolving";   RESOLVED = "resolved"
    DISMISSED = "dismissed";   EXPIRED = "expired";      SUPPRESSED = "suppressed"

class CameraState(StrEnum):
    DISABLED = "disabled";       INITIALIZING = "initializing"
    ONLINE = "online";           DEGRADED = "degraded"
    ANALYSIS_SUSPENDED = "analysis_suspended"
    RECONNECTING = "reconnecting"; OFFLINE = "offline"; FAILED = "failed"

class ZoneKind(StrEnum):
    RESTRICTED = "restricted";   EXCLUSION = "exclusion"   # ignore detections inside
    COUNTING = "counting";       CROSSING_LINE = "crossing_line"
    REGION_OF_INTEREST = "region_of_interest"

class EvidenceKind(StrEnum):
    SNAPSHOT_RAW = "snapshot_raw";       SNAPSHOT_ANNOTATED = "snapshot_annotated"
    CLIP_PRE = "clip_pre";               CLIP_POST = "clip_post"
    TRACK_DUMP = "track_dump";           SCENE_DUMP = "scene_dump"
    EXPLANATION = "explanation"
```

`EventType` lists all ten requested categories from the start, because the enum is the
taxonomy and it should be stable. Whether a category has a *module* is a separate question,
answered at runtime by the registry (04 §4). The console renders only event types with an
available module; the rest are not offered. **The enum is not a promise of functionality**,
and `docs/LIMITATIONS.md` states which types have no implementation.

`EXCLUSION` zones exist because the most effective false-positive suppression in practice
is spatial: mask off the road visible through the fence, the TV in the corner, the reflective
window. It belongs in the data model from day one.

## 4. Core types

### Camera, CameraHealth

```python
@dataclass(frozen=True, slots=True)
class Camera:
    camera_id: str                    # slug, validated (01 §7.7)
    name: str                         # operator-facing
    source: SourceSpec                # kind + locator + options; NEVER credentials
    location: str | None              # free text: "North gate, pole 3"
    priority: int                     # 0..9; drives scheduling and degradation order
    enabled: bool
    zones_ref: str | None             # path key into config, not an absolute path
    target_inference_fps: float
    tags: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class CameraHealth:
    camera_id: str
    state: CameraState
    since_wall_utc: datetime
    stream_epoch: int
    reconnect_attempts: int
    last_frame_wall_utc: datetime | None
    measured_fps: float | None
    frames_received: int
    frames_skipped: int              # intentionally not inferred (LATEST_ONLY)
    frames_dropped: int              # lost to a full queue — a problem
    decode_errors: int
    detail: str | None               # why it is in this state, in words
```

`frames_skipped` and `frames_dropped` are separate fields on purpose. Skipping is the
system working as designed; dropping is the system failing. One number for both would hide
the only one that matters.

`SourceSpec.locator` holds `rtsp://host:554/stream` with **no credentials** — they are
resolved from the secret store at connection time and never stored in a domain object, so
they cannot leak through a log, an API response, or an exported report.

### Frame

```python
@dataclass(frozen=True, slots=True)
class FrameMeta:
    camera_id: str
    stream_epoch: int
    frame_index: int                 # monotonic within an epoch
    t_monotonic_ns: int              # capture instant
    wall_utc: datetime
    width_px: int
    height_px: int
    source_pts_ms: float | None      # media timestamp, for file replay
    is_keyframe: bool | None

@dataclass(frozen=True, slots=True)
class Frame:
    meta: FrameMeta
    pixels: PixelBuffer              # protocol from core/, not numpy (02 §5)
```

`(camera_id, stream_epoch, frame_index)` is the frame identity. `stream_epoch` increments on
every reconnect, so frame indices never collide across a gap, and any downstream state keyed
by frame identity is automatically invalidated by a reconnect.

### Detection, TrackedObject

```python
@dataclass(frozen=True, slots=True)
class Detection:
    bbox: BBox                       # source-frame pixels, always (04 §3.1)
    object_class: ObjectClass
    confidence_ratio: float          # detector's own score, [0,1]
    native_label: str                # pre-normalization, kept for debugging
    mask: MaskRef | None

@dataclass(frozen=True, slots=True)
class DetectionSet:
    frame_meta: FrameMeta
    detections: tuple[Detection, ...]
    model: ModelDescriptor
    inference_latency_ms: float

@dataclass(frozen=True, slots=True)
class TrackPoint:
    t_monotonic_ns: int
    bbox: BBox
    confidence_ratio: float
    is_interpolated: bool            # true when predicted through an occlusion

@dataclass(frozen=True, slots=True)
class TrackedObject:
    track_id: int
    camera_id: str
    tracker_epoch: int
    object_class: ObjectClass
    bbox: BBox
    confidence_ratio: float
    first_seen_monotonic_ns: int
    last_seen_monotonic_ns: int
    age_frames: int
    hits: int                        # frames actually matched to a detection
    time_since_update_frames: int
    trajectory: Trajectory           # bounded ring buffer
    velocity_px_s: Vector2 | None     # None until min history
    velocity_m_s: Vector2 | None      # None without GROUND_PLANE
    zone_dwell_ms: Mapping[str, float]
    flags: frozenset[TrackFlag]      # SUSPECT_MOTION, OCCLUDED, STATIONARY, NEW, ...
```

`hits` vs `age_frames` is the track-quality signal: a track 60 frames old with 6 hits is
mostly prediction and should not support an incident. `TrackQualityValidator` uses exactly
this ratio.

`is_interpolated` on trajectory points means velocity computed across an occlusion can be
flagged rather than trusted.

### Scene

```python
@dataclass(frozen=True, slots=True)
class Zone:
    zone_id: str
    camera_id: str
    kind: ZoneKind
    polygon: Polygon                 # normalized [0,1] coordinates (04 §3.3)
    criticality: int                 # 0..4, feeds severity
    active_schedule: Schedule | None  # e.g. restricted only 18:00-06:00
    label: str

@dataclass(frozen=True, slots=True)
class ZoneOccupancy:
    zone_id: str
    counts_by_class: Mapping[ObjectClass, int]
    track_ids: frozenset[int]
    density_per_kpx2: float
    entered_track_ids: frozenset[int]   # since previous scene state
    exited_track_ids: frozenset[int]

@dataclass(frozen=True, slots=True)
class SceneState:
    frame_meta: FrameMeta
    tracks: tuple[TrackedObject, ...]
    zones: tuple[Zone, ...]
    occupancy: Mapping[str, ZoneOccupancy]
    relations: tuple[SpatialRelation, ...]    # NEAR, INSIDE, APPROACHING, CONVERGING
    stability_ratio: float                    # 1.0 = stable; drops on light/camera change
    capabilities: frozenset[Capability]
    degraded_facts: frozenset[SceneFact]
```

`entered`/`exited` are computed by the scene analyzer by diffing against the previous state,
not by each module separately. Every module that cares about zone transitions gets the same
answer, and the edge cases (a track that vanishes inside a zone, a track that teleports
across one) are handled once.

### Observation, AccumulatorState, CandidateEvent, VerifiedEvent

```python
@dataclass(frozen=True, slots=True)
class SubjectKey:
    kind: Literal["track", "zone", "region", "camera"]
    value: str
    # str form: "track:42", "zone:dock", "region:g7", "camera:"

@dataclass(frozen=True, slots=True)
class AccumulatorState:
    camera_id: str; module_id: str; subject: SubjectKey
    log_odds: float
    confidence_ratio: float
    observation_count: int
    support_ratio: float
    first_observation_ns: int
    last_observation_ns: int
    is_active: bool

@dataclass(frozen=True, slots=True)
class CandidateEvent:
    candidate_id: str                # ULID
    camera_id: str; module_id: str
    event_type: EventType
    subject: SubjectKey
    accumulator: AccumulatorState
    observations: tuple[Observation, ...]    # the window that caused promotion
    involved_track_ids: frozenset[int]
    zone_ids: frozenset[str]
    promoted_monotonic_ns: int
    promoted_wall_utc: datetime

@dataclass(frozen=True, slots=True)
class VerifiedEvent:
    candidate: CandidateEvent
    verdicts: tuple[ValidatorVerdict, ...]   # every validator that ran, in order
    confidence_ratio: float                  # post-verification, may be penalised
    verified_monotonic_ns: int
```

`verdicts` keeps **all** verdicts, including passes. "Which validators ran and said yes" is
as diagnostically valuable as the rejections, and without it you cannot tell a validator
that passed from one that never executed.

### Incident

```python
@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: str                 # ULID
    display_ref: str                 # INC-20261001-7F3A2B
    camera_id: str
    event_type: EventType
    module_id: str
    module_version: str

    status: IncidentStatus
    severity: Severity
    severity_score: float            # 0..100
    confidence_ratio: float

    zone_ids: frozenset[str]
    location: str | None             # denormalized from Camera at creation time

    first_observed_wall_utc: datetime     # earliest contributing observation
    confirmed_wall_utc: datetime          # promotion + verification
    last_observed_wall_utc: datetime
    resolved_wall_utc: datetime | None
    duration_ms: float | None

    involved_track_ids: frozenset[int]
    tracker_epoch: int

    evidence_ids: tuple[str, ...]
    explanation: IncidentExplanation
    transitions: tuple[IncidentTransition, ...]

    model_metadata: ModelDescriptor
    system_metadata: SystemSnapshot       # version, config hash, device, fps, latency
    operator_note: str | None
```

Four timestamps, each answering a different question, and the difference between them is
the system's most interesting output:

- `first_observed` — when evidence began. Earlier than confirmation. Pre-roll evidence is
  anchored here.
- `confirmed` — when the system decided. `confirmed - first_observed` is **detection
  latency**, the headline quality metric for the temporal engine.
- `last_observed` — when evidence last supported it.
- `resolved` — when it released, or when an operator closed it.

`location` is denormalized deliberately: an incident must remain readable after a camera is
renamed, moved, or deleted. The same reasoning drives `model_metadata` and
`system_metadata` being copied onto the incident rather than referenced. An incident record
is an **immutable historical fact** and must not change meaning because configuration
changed afterward.

`system_metadata.config_hash` is the sha256 of the effective resolved config. It is what
makes "why did this fire in March but not now" answerable.

### IncidentExplanation

```python
@dataclass(frozen=True, slots=True)
class IncidentExplanation:
    trigger_summary: str                             # one human sentence
    observations: tuple[ObservationSummary, ...]     # t, signal, facts
    accumulator_trace: tuple[AccumulatorSample, ...] # (t, log_odds, confidence)
    validator_verdicts: tuple[ValidatorVerdict, ...]
    severity_factors: tuple[SeverityFactor, ...]     # name, raw, weight, contribution
    thresholds: Mapping[str, float]                  # the values in force at the time
    capability_caveats: tuple[str, ...]              # "velocity in px/s: no ground plane"
```

`thresholds` is captured per incident rather than read from current config at display time.
Thresholds change as the system is tuned; an explanation that renders against today's
thresholds misrepresents a decision made under yesterday's.

### Evidence, Alert, metrics

```python
@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str                 # ULID
    incident_id: str
    kind: EvidenceKind
    relative_path: str               # relative to paths.evidence_dir — never absolute
    sha256: str
    bytes: int
    media_type: str
    captured_wall_utc: datetime
    span_ms: tuple[float, float] | None       # relative to first_observed
    expires_wall_utc: datetime | None         # from retention policy, computed at write

@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: str; incident_id: str
    severity: Severity
    created_wall_utc: datetime
    channels: tuple[str, ...]
    deliveries: tuple[AlertDelivery, ...]     # per channel: ok/failed, attempts, error

@dataclass(frozen=True, slots=True)
class SystemMetric:
    t_wall_utc: datetime
    cpu_percent: float
    memory_used_mb: float
    gpu_utilization_percent: float | None     # None when no NVML
    gpu_memory_used_mb: float | None
    gpu_temperature_c: float | None
    pipeline_fps: float
    inference_latency_p50_ms: float
    inference_latency_p95_ms: float
    queue_depths: Mapping[str, int]
    frames_dropped_total: int
```

Every GPU field is `| None`. The system must run with no GPU and the UI must render
"unavailable", not "0".

## 5. Persistence schema

SQLite, WAL mode (D-006). Tables mirror the domain types; `persistence/mappers.py` is the
only translator (02 §4).

```
cameras(camera_id PK, name, source_kind, source_locator, location, priority,
        enabled, config_json, created_utc, updated_utc)

incidents(incident_id PK, display_ref UNIQUE, camera_id FK, event_type, module_id,
          module_version, status, severity, severity_score, confidence_ratio,
          first_observed_utc, confirmed_utc, last_observed_utc, resolved_utc,
          duration_ms, zone_ids_json, location, involved_track_ids_json,
          tracker_epoch, explanation_json, model_metadata_json,
          system_metadata_json, operator_note)

incident_transitions(id PK, incident_id FK, from_status, to_status, at_utc,
                     actor, reason)            -- actor: 'system' | 'operator'

evidence(evidence_id PK, incident_id FK, kind, relative_path, sha256, bytes,
         media_type, captured_utc, span_start_ms, span_end_ms, expires_utc)

candidate_events(candidate_id PK, camera_id, module_id, event_type, subject,
                 log_odds, confidence_ratio, promoted_utc, outcome,
                 rejection_reason, verdicts_json)

alerts(alert_id PK, incident_id FK, severity, created_utc, deliveries_json)

system_metrics(t_utc PK, cpu_percent, memory_used_mb, gpu_utilization_percent,
               gpu_memory_used_mb, gpu_temperature_c, pipeline_fps,
               inference_latency_p50_ms, inference_latency_p95_ms,
               queue_depths_json, frames_dropped_total)

camera_health_events(id PK, camera_id FK, state, at_utc, detail)

schema_version(version, applied_utc)           -- alembic
```

Indexes, each justified by a query the console actually makes:

| Index | Serves |
|-------|--------|
| `incidents(confirmed_utc DESC)` | the timeline and incident list |
| `incidents(status, severity DESC)` | the incident desk's open-incident queue |
| `incidents(camera_id, confirmed_utc DESC)` | per-camera history |
| `incidents(event_type, confirmed_utc DESC)` | analytics by category |
| `evidence(incident_id)` | evidence loading |
| `candidate_events(promoted_utc DESC)` | the rejection feed (§6) |
| `system_metrics(t_utc DESC)` | the System view time series |

**Rejected candidates are persisted.** `candidate_events` records candidates that were
rejected, with the reason. This is the dataset for tuning false-positive suppression: without
it, you only ever see what got through, and you are tuning blind. Retained under
`retention.candidate_days` (default 7), which is shorter than incidents because the volume
is much higher.

### Write discipline

- Writes happen only in `ResponseWorker` and `MetricsSampler`. No other thread touches the
  database, so SQLite's writer lock is never contended.
- `system_metrics` is batched at `observability.metric_flush_s` (default 10) in one
  transaction. One INSERT per second per metric would dominate the write load for no gain.
- Reads from the API use a separate read-only connection. WAL permits concurrent readers
  with one writer, which is exactly this access pattern.
- An incident is written in one transaction with its transitions and evidence rows. A
  half-written incident must never be visible.

## 6. Evidence layout

```
var/evidence/
  <camera_id>/<YYYY>/<MM>/<DD>/<incident_id>/
      manifest.json              -- every file, with sha256, kind, span
      snapshot_raw.jpg           -- at confirmation, unannotated
      snapshot_annotated.jpg     -- boxes, zones, labels
      clip_pre.mp4               -- pre-roll, ending at first_observed
      clip_post.mp4              -- from confirmation forward
      tracks.json                -- trajectories of involved tracks
      scene.json                 -- SceneState at confirmation
      explanation.json           -- IncidentExplanation
```

Decisions behind this:

- **Date-partitioned under camera** so retention deletion is a directory walk with no
  database query, and so a filesystem browse is navigable by a human.
- **`relative_path` in the DB, never absolute** (01 §10). The data directory must be
  movable.
- **sha256 on everything**, verified on read. Evidence whose hash does not match is served
  with an explicit integrity warning rather than silently.
- **`manifest.json` per incident** so an evidence directory is self-describing if it is
  copied out of the system, which is what happens when evidence is handed to someone.
- **Pre-roll** comes from a per-camera `PreRollBuffer` of encoded JPEGs
  (07 §6), sized by `evidence.preroll_seconds` (default 5) with a hard cap at
  `evidence.preroll_max_mb` per camera (default 64). Bounded memory, always.

## 7. Configuration model

Config is data, so it belongs in this document. Shape, with representative keys:

```yaml
version: 1

paths:
  data_dir: ./var                 # everything else derives from this
  models_dir: ${paths.data_dir}/models

logging:
  level: INFO
  json_file: ${paths.data_dir}/logs/vigil.jsonl
  rotate_mb: 64
  retain_files: 10

vision:
  device: auto                    # auto | cuda | cpu
  precision: auto                 # auto | fp16 | fp32
  backend: ultralytics            # ultralytics | onnxruntime | mock | null
  weights: yolov8n.pt
  input_size: 640
  resolution_tiers: [640, 512, 416]
  max_batch_size: 4
  vram_budget_mb: 4000
  allow_cpu_fallback: true
  min_detection_confidence_ratio: 0.25
  nms_iou_ratio: 0.45
  label_map: { person: PERSON, car: VEHICLE_CAR }

tracking:
  max_age_frames: 30
  min_hits: 3
  iou_match_threshold_ratio: 0.3
  history_length: 90
  min_history_for_velocity: 5

temporal:
  half_life_ms: 2000
  signal_floor: 0.02
  l_bound: 8.0
  l_activate: 2.2
  l_release: 0.8
  confidence_scale: 1.0
  min_observations: 4
  min_support_ratio: 0.6
  min_duration_ms: 1200
  release_sustain_ms: 2000

verification:
  max_defer_ms: 10000
  cooldown_ms: 60000
  instability_blackout_ms: 3000
  min_track_hit_ratio: 0.5

severity:
  weights: { base: 0.30, zone_criticality: 0.20, confidence: 0.15,
             object_count: 0.10, persistence: 0.10, time_of_day: 0.05,
             escalation: 0.05, camera_trust: 0.05 }
  bands: { LOW: 20, MODERATE: 40, HIGH: 65, CRITICAL: 85 }

pipeline:
  global_inference_fps: 20
  analysis_queue_size: 32
  response_queue_size: 128
  degrade_sustain_ms: 3000
  min_inference_fps: 2

evidence:
  preroll_seconds: 5
  preroll_max_mb: 64
  postroll_seconds: 10
  snapshot_quality: 90

retention:
  incident_days: 365
  clip_days: 30
  snapshot_days: 90
  candidate_days: 7
  metric_days: 14

api:
  bind_host: 127.0.0.1
  bind_port: 8088
  push_interval_ms: 100
  auth: { enabled: false, token: ${env:VIGIL_API_TOKEN} }

modules:
  observe_budget_ms: 5
  quarantine_threshold: 3
  enabled: [intrusion]            # explicit; nothing auto-enables

cameras: [ { $include: cameras/*.yaml } ]
```

Properties this satisfies:

- Every threshold mentioned anywhere in this doc set resolves to a key in this model. The
  listing above is representative, not exhaustive; the complete reference is generated from
  the schema by `vigil doctor --dump-config` in P0, so documentation cannot drift from code.
  "No magic numbers" is then a mechanical property rather than an aspiration:
  `temporal.l_activate` is a config key, not a `2.2` in a source file.
- `${paths.data_dir}` and `${env:VAR}` interpolation means no absolute paths and no inline
  secrets.
- `modules.enabled` is an explicit allowlist. Discovering a module does not enable it.
- `version: 1` lets the loader migrate old config files instead of failing cryptically.
- Camera definitions are included from a glob, so adding a camera is adding a file.

## 8. Retention

Enforced by a worker on `retention.sweep_interval_s` (default 3600):

1. Delete `evidence` rows and files past `expires_wall_utc`, oldest first, logging counts
   and bytes reclaimed.
2. Delete `candidate_events` past `candidate_days`.
3. Downsample `system_metrics` past `metric_days` to hourly aggregates before deletion, so
   long-term trend is preserved at a fraction of the rows.
4. Delete `incidents` past `incident_days` — **last**, and only when their evidence is
   already gone, so an incident never references missing evidence.
5. Never delete an incident whose status is `ACTIVE`, or one with an operator note, without
   explicit operator action. An annotated incident is a human artifact.

Deletion is logged at INFO with counts. A retention run that deletes nothing logs that too;
silence is indistinguishable from a broken worker.
