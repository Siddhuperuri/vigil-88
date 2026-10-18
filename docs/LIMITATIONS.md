# Limitations

The honest gap between the architecture and the implementation. Updated at every phase.
Nothing here is hidden behind a control that appears to work (README rule 1).

**Current phase: P5 (person-down, abandoned object, analytics, reporting).**

The chain runs end to end on a live camera. Frames are captured, inferred, tracked and assembled
into a `SceneState`; modules observe it; observations accumulate as log-odds with half-life decay,
cross dual thresholds with hysteresis, pass eleven validators, and are scored into a severity
band. A verified event becomes a durable incident with its explanation, its evidence and its
audit trail, and an operator can acknowledge, dismiss, annotate or escalate it.

`tests/integration/test_incident_workflow.py` drives that whole path through a real
`Application` on real threads against a real SQLite file, and asserts that one sustained
intrusion produces **exactly one** incident, that its evidence is hashed and self-describing,
and that both survive a restart.

Three vision modules now observe that scene — `intrusion`, `person_down` and
`abandoned_object` — and every number the analytics layer reports is an aggregate over rows that
were actually persisted, with a metric that cannot be computed from the records present reported
as **unavailable rather than as zero**. A camera with four measured floor points reports velocity
in m/s as well as px/s; one without reports px/s and says so.

**P6 is not started.** Fire and smoke detection, the degradation ladder, the supervisor and the
security and dependency audit do not exist. The sections below say what else does not.

## What does not exist

| Area | State | Planned |
|------|-------|---------|
| Velocity in m/s **validated against a tape measure** | `scene/calibration.py` is built: four measured points, a homography, `GROUND_PLANE` granted per camera, and `velocity_m_s` filled at each track's foot point. **The 10%-of-ground-truth exit criterion is not met and cannot be met here** — it needs a physical camera, a tape and someone walking a measured distance. What is proven is the mathematics against synthetic geometry with known answers. An uncalibrated camera still reports px/s, labelled px/s | needs a real camera |
| Post-roll clip on a build with no MP4 encoder | Clips are muxed with OpenCV's `VideoWriter` using `mp4v`, and availability depends on what the local OpenCV build was compiled with. When no writer can be opened the clip is **absent and the failure is named** in the incident's manifest, with `evidence_status = partial`. There is no silent fallback format and no zero-byte placeholder | as needed |
| Arbitration: merge and spatial coalesce | The **subsume** and **escalate** rules of 06 §7 are implemented. **Merge** needs `FIRE`/`SMOKE` and **spatial coalesce** needs `region:` subjects; neither event type has a module | P6 |
| Sequence patterns in a shipped module | `temporal/sequence.py` implements the ordered-pattern matcher of 04 §6.4 and is unit-tested, and **still no shipped module uses it**. `abandoned_object` expresses its owner-then-gone sequence as accumulating evidence instead, on purpose: a matcher resets on the first frame the tracker blinks, while "the last time an owner was seen beside it" survives a detection gap (ADR 0014 §B). The matcher remains the right tool for patterns whose steps are discrete events | when one has them |
| Threshold tuning against real footage | **Not done.** Every value in `temporal`, `verification` and `severity` is the architecture's starting default. The tuning observations recorded below still stand, and the dismissal rate the console now records is the measurement that should drive it | next |
| Event modules beyond the three shipped | `fire`, `smoke`, `vehicle_collision`, `crowd_anomaly`, `dangerous_motion` and `vehicle_anomaly` have entries in `EventType` and no code. `EventType` is the taxonomy, not a promise of functionality (05 §3) | P6 (fire, smoke) |
| Pose estimation | Not built, and **not declared**. `POSE` is in the capability vocabulary and nothing grants it. `person_down` is bounding-box geometry and says so in its descriptor, its facts and its README; it declares no `POSE` dependency because it uses none, and an optional-but-unused capability would report a degradation that changes nothing (ADR 0014 §A) | when a module needs it |
| PDF reports without the `reports` extra | Built, behind an optional extra: `uv sync --extra reports` installs `reportlab` and `format=pdf` produces a document. Without it the format answers **503 with the install command**, and the viewer's PDF button ships disabled carrying that reason. A core renderer would cost every headless install a font stack and `pillow` for a report many deployments never ask for (ADR 0014 §C) | installed on demand |
| Degradation ladder, supervisor/restart | Not built. The scheduler bounds load, but nothing lowers resolution or demotes cameras under overload, and a crashed worker is reported rather than restarted | P6 |
| Web operations console | Not built. The **provisional viewer** at `/` now shows the live streams, the metrics, the incident list, an incident's full explanation and evidence, the real operator actions and the mute countdown. It follows the console's honesty rules (a disabled control says why) but it is not the seven-view console of 08 | later |
| RTSP / IP cameras | Config validates (and rejects inline credentials). Such a camera is reported **FAILED: not implemented** | later |
| Video-file replay, image input | Not built | later |
| Ultralytics backend | Deliberately not built (AGPL, drags in PyTorch); see ADR 0012 | only if needed |
| Windows Credential Manager (`keyring`) | Not implemented; secrets come from environment variables or `.env` | when needed |
| Alert channels beyond console, jsonl and webhook | Email, SMS and MQTT are not implemented and are **absent from the channel list**, not stubbed. A configured channel that cannot deliver reports `is_available()` false with its reason | on demand |
| Evidence inside an export | A report carries evidence **references** — ids, relative paths, sizes and hashes — never bytes. The files are fetched through the API, which is the only path that re-resolves the stored path and verifies the hash (05 §6). There is no single-file zip bundle | on demand |
| Soak testing | **Not run.** P4's exit criteria call for an 8-hour synthetic soak with bounded RSS and handle count. Not executed in this environment; the evidence ring's byte ceiling is asserted by a test over a short run only | P6 |
| Calibrated probabilities | A confidence is a **score**, not a probability. The log-odds accumulator produces a number in [0, 1] that orders events sensibly; no reliability diagram has been plotted against labelled outcomes, so "0.8" does not mean "right four times in five". Analytics never aggregates confidences into a rate, and the metric definitions say so | needs labelled data |

## Incidents and evidence: what P4 proves and what it does not

1. **The wiring is proven, end to end.** One running `Application`, real threads, real SQLite:
   frames become exactly one incident, with its explanation, its hashed evidence and its audit
   trail, surviving a restart and a clean shutdown with `PRAGMA integrity_check` returning `ok`
   and no evidence row orphaned from its incident.
2. **The failure paths are proven in isolation.** A full disk, an unwritable directory, an
   encoder that produces nothing, a write interrupted mid-flight, a tampered evidence path, a
   hash mismatch on read, a retention sweep that cannot delete a file — each is tested, and each
   produces a named failure rather than a placeholder. Evidence is never fabricated.
3. **Real-world detection performance is still unknown, and still not claimed.** P4 changes
   nothing about perception. What it adds is the *measurement*: the dismissal rate is now
   recorded per incident, which is the only honest signal about false positives (06 §9), and
   `candidate_events` keeps every rejection with the validator that caused it.
4. **The evidence memory ceiling is asserted over seconds, not hours.** `preroll_max_mb` is
   enforced by the buffer and checked by a test during a short run. The 8-hour soak that P4's
   exit criteria ask for has **not** been run.

## Event intelligence: what the P3 tests do and do not prove

Three different claims, kept apart deliberately (§27 of the P3 brief):

1. **Mathematical correctness — proven.** The update rule of 04 §6.2 is implemented exactly and
   verified by property tests over randomised inputs: `signal = 0.5` contributes exactly zero,
   `L` stays inside `[-l_bound, +l_bound]`, stronger evidence never lowers `L`, decay halves `L`
   over one half-life to within 1e-9, and an identical observation sequence yields an identical
   final state.
2. **System behaviour — proven on synthetic scenes.** A sustained intrusion produces exactly one
   event; a 300 ms boundary crossing produces none; sixty seconds of boundary oscillation
   produces at most one confirm and one resolve; two people in one zone produce two independent
   events; an unstable scene, an unhealthy camera and an operator mute each produce zero events
   with a recorded reason. These prove the suppression *mechanisms* behave as specified.
3. **Real-world detection performance — unknown, and not claimed.** No synthetic test tells you
   the false-positive rate of a real camera (09 §11). Whether these thresholds are right for a
   given scene is an empirical question, answerable only from that scene's footage and the
   dismissal rate instrumented from P4.

### Corrections to the architecture documents

Found while implementing against them. Recorded here rather than silently coded around, per the
working agreement in 10-roadmap.md §11.

- **06 §12's worked example does not sum to its own headline.** Its eight stated severity
  factors sum to **60.5**, not the 61.9 it reports. 60.5 also falls in `MODERATE`, because
  `severity.bands.HIGH` is 65 — so the example's "-> HIGH" is wrong twice over. The factor table
  and weights are normative and are what `severity/` implements; the narrative is not. Moving a
  band to make the headline true would have been tuning to a typo.
- **The same example's timings are internally inconsistent.** It states a first observation at
  `02:41:00.3` and promotion at `02:41:01.2`, then claims "elapsed 1200 ms >= 1200" — that span
  is 900 ms, and the stated detection latency of 900 ms agrees with the timestamps rather than
  with the 1200 ms claim.
- **`min_duration_ms` is implemented over the evidence span, not "since the first
  observation".** 04 §6.3 says "elapsed since first observation". Measured literally to *now*,
  that threshold becomes satisfiable by waiting: a 900 ms crossing would still confirm a few
  frames after the person left, because the decayed log-odds stays above `l_activate` and the
  support window shrinks to match. 06 §14 requires the opposite — a brief entry must produce no
  candidate — so the span measured is first-observation to last-observation. This was caught by
  a test, not by inspection.

### Tuning observations, from measurement rather than opinion

Not acted on, because tuning needs real footage (above). Recorded so the next person starts
from evidence.

- **Confidence saturates almost immediately at the shipped defaults.** With `base_signal` 0.70
  at 10 fps and `half_life_ms` 2000, each frame adds ln(0.7/0.3) = 0.847 and the steady state
  would be about 24.8 — far past `l_bound` 8.0, which `L` reaches in roughly 1.5 s. Reported
  confidence is therefore ~0.9997 for every sustained intrusion, which makes it useless for
  ranking and makes the severity engine's `confidence` factor nearly constant. `l_activate` is
  crossed at the *third* observation, so promotion is in practice governed entirely by
  `min_observations` and `min_duration_ms`. Candidates: raise `confidence_scale`, lower
  `l_bound`, or lower `base_signal`.
- **Boundary oscillation is suppressed by `min_support_ratio`, not by hysteresis.** A person
  alternating in and out of a zone every frame occupies half the support slots, so 0.5 falls
  below the 0.6 floor and nothing is ever promoted. The hysteresis gap is still correct and
  tested, but it is the second line of defence here, not the first.
- **The intrusion module's dwell bonus materially changes accumulation dynamics.** At a weak
  `base_signal` of 0.51 the module never confirms; once the +0.10 dwell bonus applies at three
  seconds, the per-frame contribution rises more than tenfold and it does. That is intended
  — persistence is evidence — but it means `dwell_bonus_ratio` is a stronger tuning knob than
  its size suggests.

### Measured cost of the event chain

Measured with `scripts/measure_p3.py` on the development machine; one stage, no model, no
camera. Not a performance claim — `docs/PERFORMANCE.md` is the only place permitted to make one
(README rule 2).

| What | p50 | p99 |
|------|-----|-----|
| One accumulator update (`observe`) | 0.1 us | 0.1 us |
| Full chain per analysed frame, 1 subject in zone | 0.1 us | 0.2 us |
| Full chain per analysed frame, 5 subjects | 0.6 us | 0.8 us |
| Full chain per analysed frame, 20 subjects | 2.4 us | 3.4 us |

Live accumulators stayed at 300 — the idle-expiry window divided by the slot width — whether
1,000 or 10,000 distinct subjects passed through, with about 585 KiB retained. The chain is
roughly four orders of magnitude cheaper than inference on this hardware, which is what 04 §2
assumes when it says the narrow stages can afford to be careful.

## The P5 modules: what they claim and what they do not

Both modules emit **observations**, not incidents (D-003). Neither decides anything: promotion
needs `temporal.l_activate` crossed while `min_observations`, `min_support_ratio` and
`min_duration_ms` all hold, and then eleven validators have to agree.

1. **`person_down` is geometry, and makes no medical claim.** It reports that a tracked person's
   box has collapsed from upright to prone and stayed that way — *not* that anyone is injured,
   unconscious or in distress. No pose model is used and none is declared; every observation
   carries `method: bounding-box geometry; no pose model`, and the descriptor and README say the
   same. The dominant false positive is someone sitting or lying down on purpose, which is
   geometrically identical to a collapse. Someone first seen already on the ground has no
   standing reference, so the collapse term is reported unavailable and the remaining weights are
   renormalised — a real gap, not a degradation that can be tuned away.
2. **`abandoned_object` requires an owner to have been seen and to have left.** Stationary alone
   is never enough — a bin is stationary. An object nobody has been observed beside is furniture
   as far as the module can tell, and it stays silent about it permanently, with no learned
   object list. The cost is the matching failure: a bag dropped before its track existed (before
   startup, before a reconnect, before re-acquisition) can never be reported, because the same
   rule covers both cases. Ownership is proximity in time and space and nothing more; there is no
   re-identification, and a lost-and-reacquired track is a new id whose separation clock restarts.
3. **Both degrade honestly through capability negotiation.** Without `VELOCITY`, `person_down`
   runs DEGRADED, drops the immobility term and renormalises; without `ZONES`,
   `abandoned_object` watches the whole frame, runs DEGRADED, and says `zones_unavailable` rather
   than quoting a location it cannot know. A missing term is renormalised, never zeroed — treating
   an uncomputable term as 0 would turn absent evidence into evidence *against*.
4. **Neither has been evaluated against footage of the thing it detects.** There is no fall
   dataset and no abandoned-luggage dataset here. The unit tests prove the arithmetic, the
   thresholds, the degradation paths and the false-positive *mechanisms* (an edge-clipped box, a
   merged detection, an owner who returns); they prove nothing about how often either fires
   correctly in a real place. Every threshold in `params.py` is a starting default. Each module's
   README lists the false positives to expect, and none of them is fixed by tuning alone.

## Calibration: what a metre means here

- **Four points, measured by hand.** `config/calibration/<ref>.calib.yaml` holds four pixel
  positions and the floor positions measured at them. That determines a homography exactly, and
  **the accuracy of the result is the accuracy of the tape measure**. Nothing in the system can
  check that, and the reprojection error cannot either: an exact four-point fit reproduces its
  own points whatever they are. What *is* checked is degeneracy — coincident points, three in a
  line — in both pixel and world space.
- **Only the foot point is converted.** A person's feet are on the calibrated floor; their head
  is a metre and a half above it. Speeds are converted at `bbox.bottom_center` and nowhere else.
- **Nothing off the plane is right, and nothing says so.** A staircase, a ramp, a thrown object,
  a vehicle on a slope: the homography answers confidently and wrongly, because a projective map
  of a plane has no way to report that its input was not on that plane.
- **Perspective is large and the system does not hide it.** Across the example calibration's own
  quad, metres-per-pixel varies by a factor of 2.5. `scale_spread()` reports that ratio, which is
  the reason a single frame-wide scale factor was refused.
- **A calibration belongs to one frame size.** Taken at 1280x720 it is refused for a 640x480
  stream, `GROUND_PLANE` is withheld and `SceneFact.GROUND_PLANE` appears in `degraded_facts` —
  rather than being rescaled by a factor nobody checked.
- **`velocity_m_s` is never computed from `velocity_px_s`.** On an uncalibrated camera it is
  `None`, not a pixel number wearing a metric label.
- **No module uses m/s yet.** Both P5 modules are deliberately scale-free: `person_down` measures
  against the track's own box height and `abandoned_object` measures distance in the object's own
  box heights, which is why both work on an uncalibrated camera. The metric velocity is reported
  for operators and for future thresholds, and nothing currently depends on it.

## Analytics: what each number is and is not

- **Every figure is an aggregate over persisted rows.** Nothing is modelled, extrapolated or
  inferred from a partial window, and no analytics query loads a table into memory — they are
  `COUNT`/`SUM`/`GROUP BY` against indexed columns, parameterised throughout.
- **Unavailable is not zero.** A metric with no records behind it (time-to-acknowledge when
  nothing has been acknowledged) comes back `available: false` with every statistic `null`, and
  the viewer renders "unavailable". On a chart a zero and an absence are indistinguishable, and
  only one of them is true.
- **A camera with no recorded health history reads as `unknown`, never as 100% online.** A blind
  camera that looks healthy is the worst thing this page could draw.
- **The dismissal rate is the dismissal rate.** It counts what operators dismissed, per module
  version. It is *not* a false-positive rate: a dismissal can be correct detection of something
  the operator did not care about, and a missed event is invisible to it. The metric definition
  travels with the number and says this.
- **There is no measured false-positive or false-negative rate, and none can be computed here.**
  That needs labelled ground truth, which does not exist in this repository.
- **Windows are closed-open and bucketed in UTC.** `[since, until)`, so adjacent windows neither
  gap nor double-count; an incident at exactly `until` is outside. A local offset in minutes only
  shifts the bucket *labels* — the arithmetic stays in UTC. A report states whether its window is
  fully covered by the records that exist (`covers_full_window`) and when the earliest record is,
  so a seven-day chart over two days of data cannot be read as seven quiet days.
- **Caveats are generated from the data, not written in advance**, and are rendered verbatim by
  both the viewer and every export.
- **Exports are JSON, CSV and (with the `reports` extra) PDF.** A saved export is written inside
  `paths.exports_dir` through the same `safe_join` the evidence store uses; a name that is not a
  plain filename is *replaced* rather than cleaned, so there is no half-sanitised string to
  reason about. CSV cells beginning `=`, `+`, `-`, `@`, tab or CR are prefixed with `'` so an
  operator note cannot become a spreadsheet formula — which is separate from, and additional
  to, correct quoting. All three formats are built from one `Report` object, so they cannot
  disagree about the window they cover.
- **A report references evidence and never embeds it.** Ids, relative paths, sizes and hashes
  go out; bytes do not. A frame copied into a PDF is evidence outside the store, with no hash
  check on the way out and nothing to verify it against later.
- **A report's row list is capped** (`rows`) and says `truncated: true` when it bites, while the
  summary above it still counts the whole window.

## Detection: what it is and what it is not

- **One model family.** YOLOX (Apache-2.0), COCO-trained, via ONNX Runtime. Three sizes are in
  the registry: `yolox_nano` and `yolox_tiny` at 416 px, `yolox_s` at 640 px.
- **Detection quality is not evaluated.** We consume published weights and have no labelled
  dataset, so there is no mAP, precision or recall figure anywhere. The only accuracy evidence is
  qualitative: the annotation was checked against one real webcam scene and a person was boxed
  correctly. Do not read the benchmark numbers as accuracy numbers.
- **Only some classes are mapped.** COCO has 80 classes; the system maps the ones it has a
  canonical meaning for (person, bicycle, car, motorcycle, bus, truck, bags, animals). Every
  other detection (chairs, books, cups ...) is dropped and counted per label in
  `vigil_unmapped_labels_total`. Cluttered scenes produce many of these; that is expected.
- **Fixed input size and static batch of 1.** These exports cannot change resolution at run time
  or process a batch. "Configurable resolution" therefore means *choosing a model* (`yolox_s`
  at 640 px or `yolox_nano`/`yolox_tiny` at 416 px). `vision.input_size_px` must equal the
  model's size or startup fails. Frames from several cameras are inferred one after another,
  so cross-camera batching gains nothing yet.
- **No FP16.** `vision.precision: fp16` is refused rather than silently ignored. On the CUDA
  provider the Ampere GPU uses TF32 for convolutions and matrix multiplies (reported as
  `fp32+tf32` in the model descriptor); this is a numerical-precision trade the provider makes.
- **Trust on first use.** Upstream publishes no checksums for the YOLOX weights, so the SHA-256
  recorded on first fetch is only as trustworthy as that first download. It is verified on every
  load afterwards.

## Hardware and performance

- **No result here is a promise.** All performance figures live in
  [PERFORMANCE.md](PERFORMANCE.md), are generated from stored measurements, and describe one
  machine: an RTX 3050 6 GB *Laptop* GPU on AC power, with a normal desktop's other applications
  running. Different power settings, battery operation, thermals or background load will change them.
- **Latency depends on how busy the GPU is.** A laptop GPU lowers its clocks when it is not
  working hard, so a sparse trickle of inferences (a few per second) runs at a fraction of the
  clock a saturated GPU holds. The same model is measurably slower per frame at low duty
  cycle. PERFORMANCE.md shows this rather than hiding it.
- **Synthetic benchmark sources** are test patterns with nothing for a detector to find, so their
  post-processing cost is that of an empty scene. The webcam runs cover real-scene cost.
- **CUDA 12 only.** ONNX Runtime 1.26 is pinned because later releases need CUDA 13, whose pip
  cuBLAS package has no Windows build. Only Windows and the one GPU above are verified.
- **Camera health trusts the driver's claimed frame rate.** The development webcam reports 30 fps
  but delivers about 15 in the indoor light of the benchmark, so the health tracker marks it
  DEGRADED for the whole run although frames arrive steadily. The webcam benchmark's
  health-issue count reflects this; the tracker should compare against the measured rate.
- **A blocked webcam read cannot be cancelled.** OpenCV's `read()` has no timeout and closing a
  capture from another thread is not safe. The watchdog detects a stall and marks the camera
  DEGRADED. Shutdown still completes: it waits `pipeline.shutdown_timeout_ms`, reports the
  straggler by name and returns. (ADR 0011.)
- **Webcam specifics.** On the development machine OpenCV 5.0 cannot open the camera through
  MSMF (`isOpened()` is false immediately), so the automatic fallback uses DirectShow, which opens
  in about 0.5 s and delivers its first frame about 0.8 s later. The driver reports no frame rate,
  so the stall threshold falls back to `ingest.stall_timeout_ms`.
- **The default global inference ceiling is 20 fps** (`pipeline.global_inference_fps`). It bounds
  total GPU load and silently caps a faster camera, by design; raise it for higher rates. The
  benchmark tooling sets it explicitly.

## API and security

- **Authentication and browsers.** With `api.auth.enabled`, every API route requires a bearer
  token (constant-time comparison; a non-loopback bind refuses to start without it). A browser
  `<img>` cannot send headers and tokens must never go in URLs, so with auth enabled the MJPEG
  stream works for programmatic clients but not for the bare `<img>` in the provisional viewer.
  The session flow that fixes this belongs to the full console.
- **MJPEG only**, at most `api.stream_fps`. WebRTC sits behind the `VideoTransport` protocol if
  measurement ever demands it.
- **One process, one operator.** No users, roles or audit trail.

## Other

- **No automatic worker restart.** A crashed worker is logged, counted and makes the application
  report FAILED health.
- **Capture-to-inference latency is only reported for live sources.** Replay cameras run on a
  PTS-driven clock that is not comparable to the application clock.
- **The synthetic source is a test pattern**, not a scene simulator.
- **Hardware coverage.** The real-webcam test is opt-in (`pytest --webcam`) because it lights the
  camera. Behaviour on other cameras and drivers is unverified.
- **Real-world false-positive rate is unknowable from here.** It needs real footage and the
  measured dismissal rate, which is now recorded but has no data behind it yet.
- **Storage failure is visible, not survivable in every form.** A write that fails is counted,
  logged and surfaced; the incident is still persisted where it can be (fail open, 01 §6). A
  database file that cannot be opened at all fails startup with the path and the reason, which
  is deliberate: running a detection system that cannot record what it finds is worse than not
  starting.
- **`storage.enabled = false` is supported and visibly different.** Events are analysed and
  nothing is stored; `GET /api/v1/system/health` reports `persists_incidents: false` and the
  viewer says so rather than showing an empty incident list as if it were good news.
