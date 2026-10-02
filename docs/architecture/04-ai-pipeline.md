# 04 — AI Pipeline Design

> Status: DESIGN. No code exists. Signatures below are the contracts P0 will create.

## 1. Canonical stage names

These eleven names are used identically in code, logs, metrics, config keys, and the UI. A
stage name never has a synonym.

```
ingest -> preprocess -> infer -> track -> scene -> observe -> accumulate
       -> promote -> verify -> score -> respond
```

| Stage | Engine | Cardinality |
|-------|--------|-------------|
| `ingest` | Ingest | per camera, continuous |
| `preprocess` | Vision | per frame selected for inference |
| `infer` | Vision | per **batch**, across cameras |
| `track` | Tracking | per camera, per inferred frame |
| `scene` | Scene | per camera, per inferred frame |
| `observe` | Event (modules) | per camera, per enabled module |
| `accumulate` | Temporal | per `(camera, module, subject)` |
| `promote` | Event | when an accumulator crosses activation |
| `verify` | Verification | per candidate |
| `score` | Severity | per verified event |
| `respond` | Response | per verified event |

Every stage records `vigil_stage_latency_ms{stage,camera}` and
`vigil_stage_errors_total{stage,camera}`. This is how the degradation ladder and the System
view get their numbers, and how "which stage is slow" is answerable without a profiler.

## 2. Why this shape

The pipeline narrows deliberately. Volume at each stage, for one 1080p camera at 25 fps
with inference at 10 fps (illustrative, not measured):

| Stage | Rate |
|-------|------|
| `ingest` | 25 frames/s |
| `infer` | 10 frames/s (scheduler drops the rest; counted as `frames_skipped`) |
| detections out | ~0–50 per frame |
| tracks out | ~0–30 per frame, with identity across frames |
| observations out | ~0–10 per frame, per enabled module |
| candidates promoted | order of 1 per minute |
| verified incidents | order of 1 per hour |

Seven orders of magnitude of reduction. The expensive stages are the wide ones, so they run
on the GPU in batches; the clever stages are the narrow ones, so they can afford to be
careful, stateful, and well-tested in pure Python. Pushing judgement upstream into the wide
stages — "alert when the detector sees fire" — is the mistake this whole architecture is
built to avoid.

## 3. Engine interfaces

All protocols live in `core/protocols/`. `Frame`, `Detection`, etc. are the frozen domain
types from [05-data-model.md](05-data-model.md).

### 3.1 Vision

```python
class Detector(Protocol):
    @property
    def descriptor(self) -> ModelDescriptor: ...
    def warmup(self) -> None: ...
    def detect_batch(self, frames: Sequence[PreparedFrame]) -> Sequence[DetectionSet]: ...
    def close(self) -> None: ...

class Classifier(Protocol):
    @property
    def descriptor(self) -> ModelDescriptor: ...
    def classify_batch(self, crops: Sequence[PreparedCrop]) -> Sequence[ClassScores]: ...

class Segmenter(Protocol):
    def segment_batch(self, frames: Sequence[PreparedFrame]) -> Sequence[MaskSet]: ...
```

Notes that matter:

- `detect_batch` is the only inference entry point. There is no `detect(frame)`
  convenience method, because its existence invites per-frame calls that defeat batching
  (D-004). A single frame is a batch of one.
- Returned coordinates are **always in source-frame pixel space**, never in the letterboxed
  model space. Un-letterboxing is the backend's job, done once, immediately after
  postprocess. Leaking model-space coordinates upward causes zone-geometry bugs that are
  miserable to find.
- `warmup()` runs the model on a synthetic batch at startup so the first real frame does
  not pay cuDNN autotuning and allocator warm-up cost. Without it, startup latency gets
  misattributed to the camera.
- `ModelDescriptor` carries name, version, weights sha256, input size, precision, device,
  and licence. It is copied onto every `Incident` as `model_metadata`, so an incident from
  six months ago can be explained.

### 3.2 Tracking

```python
class Tracker(Protocol):
    def update(self, detections: DetectionSet, frame_meta: FrameMeta) -> Sequence[TrackedObject]: ...
    def reset(self) -> None: ...
    @property
    def epoch(self) -> int: ...
```

One `Tracker` instance per camera, owned by the `AnalysisWorker` (D-005). `reset()` bumps
`epoch`; every downstream consumer treats a new epoch as "all identities are new", which is
what must happen after a reconnect or a long gap. Silent identity reuse across a gap
produces fictional trajectories, which produce fictional velocities, which produce
fictional incidents.

`TrackedObject` carries a bounded trajectory ring buffer (`tracking.history_length`,
default 90 points), velocity in px/s, `age_frames`, `hits`, `time_since_update`, and
`dwell_ms` per zone.

### 3.3 Scene

```python
class SceneAnalyzer(Protocol):
    def analyze(
        self,
        tracks: Sequence[TrackedObject],
        frame_meta: FrameMeta,
        zones: ZoneSet,
    ) -> SceneState: ...
```

`SceneState` is an immutable snapshot (05 §4): tracks, zone occupancy, spatial relations,
density estimates, scene-stability signal, and `degraded_facts: frozenset[SceneFact]`
listing facts that could not be computed this frame. Modules check `degraded_facts` rather
than discovering `None` values at use time.

Zones are stored in **normalized [0,1] coordinates** so a resolution change or a degradation
step does not invalidate them. Conversion to pixels happens once per frame in the analyzer.

### 3.4 Temporal

```python
class Accumulator(Protocol):
    def observe(self, obs: Observation) -> AccumulatorState: ...
    def decay_to(self, t_monotonic_ns: int) -> AccumulatorState: ...
    @property
    def state(self) -> AccumulatorState: ...
```

Full mechanism in §6.

### 3.5 Event module — the plugin contract

```python
class DetectionModule(Protocol):
    @property
    def descriptor(self) -> ModuleDescriptor: ...
    def bind(self, ctx: ModuleContext) -> None: ...
    def observe(self, scene: SceneState) -> Sequence[Observation]: ...
    def release(self) -> None: ...
```

```python
@dataclass(frozen=True)
class ModuleDescriptor:
    module_id: str                        # "intrusion"
    event_type: EventType
    version: str
    requires: frozenset[Capability]
    optional: frozenset[Capability]
    default_params: Mapping[str, object]
    description: str
```

The contract, stated as rules a module must obey:

1. `observe()` MUST be a function of `scene` and the module's own declared state. It MUST
   NOT read the clock, the filesystem, the network, or global state.
2. `observe()` MUST NOT create incidents, write evidence, or send alerts. It returns
   observations. That is all it does (D-003).
3. `observe()` MUST return within `modules.observe_budget_ms` (default 5). Exceeding it is
   logged and counted; three consecutive violations suspend the module.
4. A module MAY keep state across calls, but that state MUST be bounded and MUST be reset
   on `tracker_epoch` change.
5. Raising from `observe()` is caught by the engine. The module is quarantined after
   `modules.quarantine_threshold` (default 3) consecutive errors, reported in the console,
   and retried after `modules.quarantine_retry_ms`. One bad module never takes the
   pipeline down.

This contract is what makes incident categories genuine plugins. A new category is one
directory under `events/builtin/`, one descriptor, one `observe()`, and a fixture file — no
edits anywhere else in the codebase.

### 3.6 Verification, severity, response

```python
class EventValidator(Protocol):
    @property
    def validator_id(self) -> str: ...
    def validate(self, candidate: CandidateEvent, ctx: VerificationContext) -> ValidatorVerdict: ...

class SeverityEngine(Protocol):
    def score(self, event: VerifiedEvent, ctx: SeverityContext) -> SeverityAssessment: ...

class AlertChannel(Protocol):
    @property
    def channel_id(self) -> str: ...
    def is_available(self) -> bool: ...
    def send(self, alert: Alert) -> AlertDelivery: ...
```

`ValidatorVerdict` is `PASS | REJECT | DEFER` with a mandatory human-readable reason.
`DEFER` means "not yet decidable" — the candidate stays pending for up to
`verification.max_defer_ms` (default 10000). `DEFER` is what lets a validator wait for
evidence (e.g. "this track needs another 500 ms of history") without the verifier having to
model time itself.

`is_available()` is why alert channels cannot be fake: an unavailable channel is shown as
unavailable in the console and does not accept sends. Channels listed in
[02-directory-structure.md](02-directory-structure.md) that are not implemented do not
exist as stubs — they appear in `docs/LIMITATIONS.md` instead.

## 4. Capability model (D-008)

```python
class Capability(StrEnum):
    DETECTION = "detection"
    CLASSIFICATION = "classification"
    SEGMENTATION = "segmentation"
    TRACKING = "tracking"
    VELOCITY = "velocity"            # tracking + enough history
    POSE = "pose"
    ZONES = "zones"                  # camera has zones defined
    GROUND_PLANE = "ground_plane"    # homography calibrated; enables m/s and real areas
    OPTICAL_FLOW = "optical_flow"
    SCENE_STABILITY = "scene_stability"
```

At startup, `pipeline/graph.py` computes the capability set each camera can actually supply,
from the configured detector, tracker, zone definitions, and calibration. Then, per camera,
each enabled module is either:

- **ACTIVE** — all `requires` satisfied;
- **DEGRADED** — `requires` satisfied, some `optional` missing (recorded on every incident
  it produces, so the quality caveat travels with the data);
- **UNAVAILABLE** — a `requires` capability is missing. The module does not run. The console
  shows the module greyed out with the exact missing capability and the action that would
  fix it, e.g. *"needs GROUND_PLANE — calibrate this camera in Zones"*.

This is the anti-fake-functionality mechanism. Nothing can appear to work while silently
producing nothing, because unavailability is computed, displayed, and explained.

## 5. Inference scheduling and batching

### 5.1 Admission control

Each camera has a token bucket refilled at `camera.target_inference_fps`. The
`InferenceScheduler` loop:

1. Collect cameras with a token and a fresh frame in their `LatestFrameSlot`.
2. Sort by `(priority desc, time_since_last_inference desc)` — the second term prevents a
   low-priority camera from being starved forever.
3. Take up to `vision.max_batch_size` (default 4).
4. Preprocess into a pinned-memory batch tensor, infer once, un-batch.
5. Consume tokens, record latency, dispatch each result to the analysis queue.

Global ceiling: `pipeline.global_inference_fps`. The sum of per-camera targets may exceed it;
the scheduler is the authority and the degradation ladder reduces targets.

### 5.2 Why batch across cameras rather than within one

Within one camera, batching means waiting for N frames, adding N/fps of latency to every
detection. Across cameras, a batch is assembled from frames that already exist, so batching
costs nothing in latency and buys GPU occupancy. With one camera, batch size is naturally 1
and that is correct.

### 5.3 Preprocess

Letterbox to `vision.input_size` (tiers 640/512/416) preserving aspect ratio, BGR->RGB,
scale to [0,1], HWC->CHW, stack, cast to FP16 when `vision.precision=fp16`, copy to device
from pinned memory.

The letterbox transform (scale, pad-x, pad-y) is carried with each result so postprocess can
invert it exactly. This is recomputed per frame rather than cached, because a resolution
tier change mid-stream would silently invalidate a cached transform.

### 5.4 Precision

`vision.precision` is `fp32 | fp16 | auto`. `auto` selects fp16 on CUDA, fp32 on CPU. FP16
roughly halves activation memory and is faster on Ampere tensor cores. It is *not* applied
to NMS or coordinate arithmetic, where fp32 is kept — a 0.5 px error from fp16 rounding
near a zone boundary is exactly the kind of bug that produces intermittent, unreproducible
false positives.

INT8 quantisation is deliberately deferred: it needs a calibration dataset and changes
detection quality in ways that must be measured against a baseline we do not yet have.

## 6. Temporal confidence (D-007)

The core of "reasoning across time". This section is the specification; the implementation
in `temporal/accumulator.py` must match it exactly, and the tests in §9 verify it does.

### 6.1 Observations

A module emits, per frame, zero or more:

```python
@dataclass(frozen=True)
class Observation:
    camera_id: str
    module_id: str
    subject: SubjectKey          # track:42 | zone:dock | region:g7 | camera
    signal: float                # [0,1] — support for the hypothesis THIS FRAME
    t_monotonic_ns: int          # from the frame, never the wall clock
    facts: Mapping[str, float | str | bool]   # inputs that produced the signal
```

`signal` is explicitly **not** a probability that the incident is occurring. It is this
frame's support for the hypothesis. The distinction matters: a module should emit 0.6 for
"a person is in the restricted zone, moderately confident" every frame, and let
accumulation decide — rather than trying to output a calibrated incident probability from
one frame, which it cannot do.

Observations are keyed by `(camera_id, module_id, subject)`. That tuple owns one
accumulator.

### 6.2 Accumulation

Each accumulator holds a log-odds value `L`, initially 0 (even odds, no evidence).

On an observation at time `t`, with `dt_ms` since the last update:

```
# 1. Decay toward zero by half-life.
L <- L * 2 ** (-dt_ms / temporal.half_life_ms)

# 2. Clamp the signal away from the asymptotes so one frame cannot dominate.
s <- clamp(signal, temporal.signal_floor, 1 - temporal.signal_floor)

# 3. Add this frame's evidence, weighted.
L <- L + module_weight * ln(s / (1 - s))

# 4. Bound the total so a long quiet period cannot be instantly overcome,
#    and so a stuck-high signal cannot build an unfalsifiable case.
L <- clamp(L, -temporal.l_bound, +temporal.l_bound)
```

Properties this gives, each of which is a property test:

- **Monotonic in evidence**: a stronger signal never lowers `L`.
- **Symmetric**: `signal = 0.5` contributes exactly 0 — genuine ambiguity does not
  accumulate in either direction. This matters, because a module that is unsure should not
  be able to confirm an incident by being unsure for a long time.
- **Decay, not a window edge**: evidence fades smoothly. No incident appears or vanishes
  because a fixed window boundary moved past a frame.
- **Bounded**: `L` is always in `[-l_bound, +l_bound]`, so recovery time from a saturated
  state is bounded and predictable.

Reported confidence is a logistic mapping, used for display and thresholds:

```
confidence = 1 / (1 + exp(-L / temporal.confidence_scale))
```

Both `L` and `confidence` are persisted. `confidence` is calibrated-looking but is **not** a
calibrated probability, and the UI labels it "confidence", never "probability". Claiming
calibration without a reliability study would be a lie with a decimal point on it.

### 6.3 Promotion — dual-threshold hysteresis

A candidate is promoted when **all** hold:

| Condition | Config key | Default |
|-----------|-----------|---------|
| `L >= l_activate` | `temporal.l_activate` | 2.2 (confidence ~0.90 at scale 1.0) |
| observations in window >= min count | `temporal.min_observations` | 4 |
| supported fraction of window >= ratio | `temporal.min_support_ratio` | 0.6 |
| elapsed since first observation >= min | `temporal.min_duration_ms` | 1200 |

`min_support_ratio` is the specific defence against a flickering detector: four strong
frames scattered through fifteen seconds of nothing must not confirm, even if decay alone
would have allowed it.

An active accumulator releases when `L <= l_release` (`temporal.l_release`, default 0.8)
continuously for `temporal.release_sustain_ms` (default 2000). `l_release < l_activate` is
the hysteresis gap, and it is what stops an incident from flapping confirmed/resolved while
a person stands on a zone boundary.

Defaults are starting points for tuning against recorded footage in P3, not tuned values.

### 6.4 Sequences

Some events are orderings, not states: *door opens, then nobody appears* or *vehicle stops,
then person exits, then vehicle departs without them*. `temporal/sequence.py` provides a
small ordered-pattern matcher over observation history:

```python
SequencePattern(
    steps=[Step("stopped", min_duration_ms=3000), Step("person_exit"), Step("departed")],
    within_ms=120_000,
    allow_gaps=True,
)
```

A matched sequence emits a single high-signal `Observation`, so it feeds the same
accumulation path rather than bypassing it. There is exactly one promotion mechanism.

## 7. Label normalization

Model-native class names are a model's business, not the system's. `vision/labels.py` maps
them to a canonical `ObjectClass` enum via config:

```yaml
vision:
  label_map:
    person: PERSON
    car: VEHICLE_CAR
    truck: VEHICLE_TRUCK
    bicycle: CYCLE_BICYCLE
    backpack: OBJECT_BAG
    handbag: OBJECT_BAG
    suitcase: OBJECT_BAG
```

Unmapped labels are dropped and counted per label (`vigil_unmapped_labels_total{label}`),
so a detector swap that changes the label space shows up as a metric spike instead of as
modules mysteriously going quiet. Modules reference `ObjectClass.PERSON` and never a raw
string, so swapping YOLO for RT-DETR touches one config file.

## 8. Failure and degradation behaviour

| Failure | Behaviour |
|---------|-----------|
| CUDA OOM during inference | Catch, halve batch size, retry once; on a second failure drop the resolution tier and log at ERROR. Three in `vision.oom_window_s` -> device marked unhealthy, fall back to CPU if `vision.allow_cpu_fallback`, else cameras go `ANALYSIS_SUSPENDED` |
| Model file missing or hash mismatch | Startup failure with the expected path and hash. Never a silent download |
| Inference latency over budget | Degradation ladder (01 §9) |
| Tracker produces an implausible velocity | `MotionSanityValidator` rejects at verify; the track is flagged `suspect_motion`, not deleted |
| A module raises | Quarantine that module (§3.5) |
| Zone file invalid | Camera starts with `ZONES` capability absent; zone-dependent modules become UNAVAILABLE with the parse error as the reason |
| Scene instability (global brightness or histogram shift beyond threshold) | `SCENE_STABILITY` fact set false; `SceneStabilityValidator` rejects candidates for `verification.instability_blackout_ms`. This is what suppresses the lights-switched-on and camera-bumped false-positive storms |

## 9. How this design is proven correct

Not a testing plan ([09-testing-strategy.md](09-testing-strategy.md) owns that) — these are
the specific properties of *this* document that must be mechanically verified:

| Property | Test |
|----------|------|
| `signal = 0.5` never changes `L` | Property test over random sequences |
| `L` stays within `[-l_bound, l_bound]` | Property test over adversarial sequences |
| A stronger signal sequence never yields lower final `L` | Property test, two sequences compared pointwise |
| Decay is exact | `ManualClock`: one observation, advance one half-life, assert `L` halved to within 1e-9 |
| Hysteresis cannot flap | Signal oscillating across the activation boundary for 60 s -> at most one confirm and one resolve |
| `min_support_ratio` blocks flicker | 4 strong frames spread over 15 s -> no promotion |
| Coordinates are source-space | Letterboxed 16:9 input into a square model; a detection at a known pixel comes back at that pixel |
| Capability gating is honest | A module requiring `GROUND_PLANE` on an uncalibrated camera never runs, and the API reports it UNAVAILABLE with a reason |
| Batching is equivalent to per-frame | The same frames via batch 1 and batch 4 give identical detections within fp tolerance |
| Epoch reset clears state | After `tracker.reset()`, no accumulator retains pre-reset evidence |
