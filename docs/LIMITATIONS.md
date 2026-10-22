# Limitations

The honest gap between the architecture and the implementation. Updated at every phase.
Nothing here is hidden behind a control that appears to work (README rule 1).

**Current phase: P6 (perception and hardening).**

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

**P6 found that several things this file described as working did not.** Auditing the running
system against `07` and `06` turned up ten defects that the unit suite could not see, because every
one is a property of how two components behave together over time: a camera whose driver
overstated its frame rate was held DEGRADED for ever and therefore **detected nothing**; a
reconnect never escalated its backoff and never reached FAILED; a hung worker reported healthy;
`frames_dropped` was structurally always zero; `vigil run --selftest` was silently losing 27 of
its 60 frames; and 06 §7's **subsume** rule, which this file described as implemented, computed a
suppression that nothing acted on. All ten are fixed and pinned by tests. ADR 0015 §B lists them.

**Fire and smoke are still not detected, and now say so out loud.** No model with a compatible
licence, checkable provenance and a published held-out evaluation could be sourced, so what ships
is a *declaration* rather than a detector — see below. The degradation ladder and automatic worker
restart remain unbuilt. The sections below say what else does not.

## What does not exist

| Area | State | Planned |
|------|-------|---------|
| Velocity in m/s **validated against a tape measure** | `scene/calibration.py` is built: four measured points, a homography, `GROUND_PLANE` granted per camera, and `velocity_m_s` filled at each track's foot point. **The 10%-of-ground-truth exit criterion is not met and cannot be met here** — it needs a physical camera, a tape and someone walking a measured distance. What is proven is the mathematics against synthetic geometry with known answers. An uncalibrated camera still reports px/s, labelled px/s | needs a real camera |
| Post-roll clip on a build with no MP4 encoder | Clips are muxed with OpenCV's `VideoWriter` using `mp4v`, and availability depends on what the local OpenCV build was compiled with. When no writer can be opened the clip is **absent and the failure is named** in the incident's manifest, with `evidence_status = partial`. There is no silent fallback format and no zero-byte placeholder | as needed |
| Arbitration: spatial coalesce | **Subsume**, **escalate** and **merge** are implemented, and a suppression is now *acted on* — before P6 `suppressed_by` was computed and only logged, so subsume suppressed nothing and this file was wrong to call it implemented (ADR 0015 §B8). **Merge** has no producer in this build, because `fire` and `smoke` ship as declarations: it is unit-tested and reachable only by a third-party module. **Spatial coalesce** needs `region:` subjects, which no module emits, and is not built — so merge joins events on the identical subject and nothing wider | when a module emits regions |
| Sequence patterns in a shipped module | `temporal/sequence.py` implements the ordered-pattern matcher of 04 §6.4 and is unit-tested, and **still no shipped module uses it**. `abandoned_object` expresses its owner-then-gone sequence as accumulating evidence instead, on purpose: a matcher resets on the first frame the tracker blinks, while "the last time an owner was seen beside it" survives a detection gap (ADR 0014 §B). The matcher remains the right tool for patterns whose steps are discrete events | when one has them |
| Threshold tuning against real footage | **Not done.** Every value in `temporal`, `verification` and `severity` is the architecture's starting default. The tuning observations recorded below still stand, and the dismissal rate the console now records is the measurement that should drive it | next |
| **Fire and smoke detection** | **Not implemented, and declared rather than absent.** `events/builtin/fire/` and `smoke/` exist as *declarations*: each requires `CLASSIFICATION`, which `core/capabilities.py::UNGRANTABLE` records that **nothing in this build can grant**, so the registry reports both UNAVAILABLE on every camera with an actionable reason, and `observe()` **raises** rather than returning no observations — silence would be indistinguishable from "no fire". No colour or flicker heuristic is shipped in their place. P6 surveyed 22 models: the accurate ones are AGPL-3.0 Ultralytics derivatives (refused by ADR 0012), the one permissive ONNX candidate is a whole-image scene classifier with unnamed training data whose metrics are never stated to come from a held-out split, and several carry no licence at all. 10-roadmap §9 allows exactly two outcomes and this is the second: **no measured precision or recall exists, because nothing was shipped to measure** (ADR 0015 §A) | needs a licensed, evaluated model |
| Event modules beyond the three shipped | `vehicle_collision`, `crowd_anomaly`, `dangerous_motion` and `vehicle_anomaly` have entries in `EventType` and no code. `EventType` is the taxonomy, not a promise of functionality (05 §3) | on demand |
| Pose estimation | Not built, and **not declared**. `POSE` is in the capability vocabulary and nothing grants it. `person_down` is bounding-box geometry and says so in its descriptor, its facts and its README; it declares no `POSE` dependency because it uses none, and an optional-but-unused capability would report a degradation that changes nothing (ADR 0014 §A) | when a module needs it |
| PDF reports without the `reports` extra | Built, behind an optional extra: `uv sync --extra reports` installs `reportlab` and `format=pdf` produces a document. Without it the format answers **503 with the install command**, and the viewer's PDF button ships disabled carrying that reason. A core renderer would cost every headless install a font stack and `pillow` for a report many deployments never ask for (ADR 0014 §C) | installed on demand |
| Degradation ladder, supervisor/restart | **Still not built.** The scheduler bounds load, but nothing lowers resolution or demotes cameras under overload. A crashed worker is reported, counted and makes the application report FAILED; a worker that is *alive but no longer progressing* is now detected too (`pipeline.worker_stall_timeout_ms`, ADR 0015 §B3) and reported the same way — **detected, not restarted** | when overload is observed in a real deployment |
| Web operations console | Not built. The **provisional viewer** at `/` now shows the live streams, the metrics, the incident list, an incident's full explanation and evidence, the real operator actions and the mute countdown. It follows the console's honesty rules (a disabled control says why) but it is not the seven-view console of 08 | later |
| RTSP / IP cameras | Config validates (and rejects inline credentials). Such a camera is reported **FAILED: not implemented** | later |
| Video-file replay, image input | Not built | later |
| Ultralytics backend | Deliberately not built (AGPL, drags in PyTorch); see ADR 0012 | only if needed |
| Windows Credential Manager (`keyring`) | Not implemented; secrets come from environment variables or `.env` | when needed |
| Alert channels beyond console, jsonl and webhook | Email, SMS and MQTT are not implemented and are **absent from the channel list**, not stubbed. A configured channel that cannot deliver reports `is_available()` false with its reason | on demand |
| Evidence inside an export | A report carries evidence **references** — ids, relative paths, sizes and hashes — never bytes. The files are fetched through the API, which is the only path that re-resolves the stored path and verifies the hash (05 §6). There is no single-file zip bundle | on demand |
| Soak testing and failure drills | **Not run.** P4's exit criteria call for an 8-hour synthetic soak with bounded RSS and handle count, and P6's call for a 24-hour soak with real cameras plus documented recovery drills (unplug, disk full, model deleted, config corrupted, GPU driver reset). **None of these was executed.** The longest sustained run in this repository is 120 s, and the evidence ring's byte ceiling is asserted by a test over a short run only. The reconnect, stall, epoch and overload paths are covered by unit tests that drive them deterministically, which is not the same as having survived them in the field | needs an environment that can be left running |
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

## Camera and pipeline reliability: what P6 found and what it did not fix

The audit's full findings are in ADR 0015 §B. What matters here is which of them had been
*invisible*, and what remains deliberately unbuilt.

1. **Four of the ten were silent failures of instrumentation, not of logic.** `frames_dropped`
   was never written by any production code, worker heartbeats were recorded and never read,
   `SpillQueue` lost spill counts under concurrency, and arbitration's suppressions were computed
   and discarded. Each made the system *look* healthier than it was, and one of them was hiding
   another: wiring the drop counter immediately revealed that the inference-to-analysis queue had
   been dropping frames from PACED sources, which breaks the replay determinism 07 §2 defines
   PACED by — `vigil run --selftest` had been losing 27 of its 60 frames.
2. **The most consequential was a one-line comparison.** A camera whose driver advertises 30 fps
   and delivers 14 was held DEGRADED permanently, and DEGRADED makes verification reject every
   candidate (06 §6 validator 1) — so an optimistic driver silenced the entire system, with
   `low fps` as the only symptom. The development webcam is such a camera.
3. **These are fixes to mechanisms, not evidence that the mechanisms are right.** Every one is
   pinned by a unit test that drives it deterministically. None has been exercised by a real
   camera being unplugged, a real network blip, or a real disk filling up.

**Deliberately not built**, recorded as decisions rather than omissions:

- **Pre-roll gap annotation.** 07 §5 keeps the pre-roll buffer across a reconnect and says the
  manifest should mark that the footage has a hole. It does not. An operator reviewing a clip that
  spans a reconnect is not told that frames are missing from it.
- **`stream_interrupted` on open incidents.** 07 §5 says an incident open across a stream gap
  should be left open and annotated, so an operator sees that its evidence is incomplete. The
  string exists nowhere in the tree. An epoch reset drops pending verification and resets the
  ledger for that camera; an already-persisted ACTIVE incident is simply no longer extended.
- **Force-closing a blocked read.** 07 §5 asks the watchdog to force-close a stalled source.
  ADR 0011 records that releasing an OpenCV capture from another thread is not safe, so a stall is
  *detected* and not cancelled. What is new is that the stalled worker is now visible in the
  application's health rather than only in the camera's.

## Security posture: what was checked, and the two findings left open

Everything below was verified against the code, not asserted. Details in ADR 0015 §C.

- **Verified as claimed:** parameterised queries throughout (every SQL interpolation is a literal
  in its own module, a run of `?`, or an allowlisted sort column); no path traversal in evidence or
  exports (`safe_join` enforces a strict component pattern *and* post-symlink containment); no
  credentials or machine-specific paths in source, config, docs or the committed benchmark
  results; loopback-by-default with a bearer token required for any other bind, compared in
  constant time.
- **Two gaps found and closed.** The ruff S608 exemption covered `analytics/` as well as
  `persistence/` and only the latter had a test paying for it — `analytics/` now has one, including
  an AST check that every fragment interpolated into SQL at every call site is a literal, which is
  what survives someone threading an API parameter through later. And "no full image frames in
  logs" held only because no caller had ever logged one: `redact_value` now summarises any
  bytes-like value over 256 bytes and anything array-like, so it holds by construction.
- **Finding: the GPU extra is not all permissive.** There is no AGPL or GPL dependency anywhere,
  and `reportlab` is BSD — but `onnxruntime-gpu[cuda,cudnn]` pulls five NVIDIA CUDA
  redistributables under the proprietary CUDA EULA. Redistribution of the runtime libraries is
  permitted under it; the point is that the stack is not wholly Apache/BSD and should not be
  described as if it were. The CPU extra has no such dependency.
- **Finding: this project declares no licence of its own.** There is no `license` field in
  `pyproject.toml` and no `LICENSE` file, so by default no rights are granted to anyone. Left open
  deliberately: choosing a licence is the owner's decision, not an implementation phase's.

## Packaging: what an install gets

- **`uv build` produces an installable wheel**, carrying the `vigil` console script and all five
  module READMEs (the contract suite reads them at runtime).
- **A bare install now starts.** Until P6 it did not: `cli/main.py` imports every command, the
  chain reached `pipeline/annotate.py`, and its module-level `import cv2` meant a core-only
  install died with a `ModuleNotFoundError` traceback — taking `vigil doctor`, the command an
  operator runs *because* something is missing, down with it. The two modules on that chain now
  import cv2 and numpy lazily and refuse at their entry points naming the extra to install.
  Verified in a clean virtual environment.
- **No Docker image and no installer.** 10-roadmap §10 declines Docker for GPU-passthrough
  friction on Windows. Deployment is `uv sync` in a checkout, or `pip install` of the wheel plus
  the extras the deployment needs.

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
- **Camera health no longer trusts the driver's claimed frame rate.** The development webcam
  reports 30 fps and delivers about 15 in indoor light, which used to mark it DEGRADED for an
  entire run although frames arrived steadily — and DEGRADED makes verification reject every
  candidate, so the camera detected nothing at all. The expectation now starts at the driver's
  claim and is replaced, once per stream epoch, by what the camera has actually sustained, with
  `health.min_absolute_fps` as a floor. The webcam benchmark in this file predates the fix and its
  health-issue count still reflects the old behaviour. (ADR 0015 §B1.)
- **A blocked webcam read cannot be cancelled.** OpenCV's `read()` has no timeout and closing a
  capture from another thread is not safe. The watchdog detects a stall and marks the camera
  DEGRADED; since P6 the stalled *worker* is also visible in the application's health rather than
  only in the camera's. Shutdown still completes: it waits `pipeline.shutdown_timeout_ms`, reports
  the straggler by name and returns. (ADR 0011, ADR 0015 §B3.)
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
  report FAILED health, and so now is one that is alive but has stopped making progress for
  `pipeline.worker_stall_timeout_ms`. Neither is restarted; an operator has to.
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
