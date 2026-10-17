# Limitations

The honest gap between the architecture and the implementation. Updated at every phase.
Nothing here is hidden behind a control that appears to work (README rule 1).

**Current phase: P4 (incidents, persistence, evidence), on a now-complete P2.**

The chain runs end to end on a live camera. Frames are captured, inferred, tracked and assembled
into a `SceneState`; modules observe it; observations accumulate as log-odds with half-life decay,
cross dual thresholds with hysteresis, pass eleven validators, and are scored into a severity
band. A verified event becomes a durable incident with its explanation, its evidence and its
audit trail, and an operator can acknowledge, dismiss, annotate or escalate it.

`tests/integration/test_incident_workflow.py` drives that whole path through a real
`Application` on real threads against a real SQLite file, and asserts that one sustained
intrusion produces **exactly one** incident, that its evidence is hashed and self-describing,
and that both survive a restart.

**P5 and P6 are not started.** `person_down`, `abandoned_object`, analytics, reporting and
fire/smoke do not exist. The sections below say what else does not.

## What does not exist

| Area | State | Planned |
|------|-------|---------|
| Ground-plane calibration, velocity in m/s | Not built. `scene/calibration.py` does not exist, `GROUND_PLANE` is never granted, and every reported velocity is in **px/s and labelled as such** — in the API, in `tracks.json`, and in the UI. 10-roadmap lists this under P5; the P4 brief did not ask for it | P5 |
| Post-roll clip on a build with no MP4 encoder | Clips are muxed with OpenCV's `VideoWriter` using `mp4v`, and availability depends on what the local OpenCV build was compiled with. When no writer can be opened the clip is **absent and the failure is named** in the incident's manifest, with `evidence_status = partial`. There is no silent fallback format and no zero-byte placeholder | as needed |
| Arbitration: merge and spatial coalesce | The **subsume** and **escalate** rules of 06 §7 are implemented. **Merge** needs `FIRE`/`SMOKE` and **spatial coalesce** needs `region:` subjects; neither event type has a module | P6 |
| Sequence patterns in a shipped module | `temporal/sequence.py` implements the ordered-pattern matcher of 04 §6.4 and is unit-tested, but no shipped module uses it yet | P5 (`abandoned_object`) |
| Threshold tuning against real footage | **Not done.** Every value in `temporal`, `verification` and `severity` is the architecture's starting default. The tuning observations recorded below still stand, and the dismissal rate the console now records is the measurement that should drive it | next |
| Event modules beyond `intrusion` | `person_down`, `abandoned_object`, `fire`, `smoke` and the rest have entries in `EventType` and no code. `EventType` is the taxonomy, not a promise of functionality (05 §3) | P5, P6 |
| Analytics and reporting | Not built. No `analytics/` package, no aggregations, no CSV/JSON export, no Analytics view | P5 |
| Degradation ladder, supervisor/restart | Not built. The scheduler bounds load, but nothing lowers resolution or demotes cameras under overload, and a crashed worker is reported rather than restarted | P6 |
| Web operations console | Not built. The **provisional viewer** at `/` now shows the live streams, the metrics, the incident list, an incident's full explanation and evidence, the real operator actions and the mute countdown. It follows the console's honesty rules (a disabled control says why) but it is not the seven-view console of 08 | later |
| RTSP / IP cameras | Config validates (and rejects inline credentials). Such a camera is reported **FAILED: not implemented** | later |
| Video-file replay, image input | Not built | later |
| Ultralytics backend | Deliberately not built (AGPL, drags in PyTorch); see ADR 0012 | only if needed |
| Windows Credential Manager (`keyring`) | Not implemented; secrets come from environment variables or `.env` | when needed |
| Alert channels beyond console, jsonl and webhook | Email, SMS and MQTT are not implemented and are **absent from the channel list**, not stubbed. A configured channel that cannot deliver reports `is_available()` false with its reason | on demand |
| Incident export bundles | `POST`-able operator actions, the evidence manifest and per-file download exist. The single-file **report bundle** of 06 §10 (`analytics/export.py`) does not | P5 |
| Soak testing | **Not run.** P4's exit criteria call for an 8-hour synthetic soak with bounded RSS and handle count. Not executed in this environment; the evidence ring's byte ceiling is asserted by a test over a short run only | P6 |

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
