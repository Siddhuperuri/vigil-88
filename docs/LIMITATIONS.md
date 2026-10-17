# Limitations

The honest gap between the architecture and the implementation. Updated at every phase.
Nothing here is hidden behind a control that appears to work (README rule 1).

**Current phase: P3 (event intelligence), on an incomplete P2.** The event chain is built and
verified: observations accumulate as log-odds with half-life decay, cross dual thresholds with
hysteresis, pass eleven validators, and are scored into a severity band. The first production
module (`intrusion`) is implemented and passes the module contract suite.

**P3 runs from `SceneState` onward, and nothing yet produces a `SceneState` from a camera.**
P2 is partially implemented in this tree: zone configuration and a Kalman filter exist, but the
tracker's association and lifecycle, and the whole `scene/` engine (occupancy, relations,
stability, `SceneState` assembly) do not. So the chain is exercised by scripted scenes in tests
and is **not wired into the live pipeline**. On a running camera, `TRACKING`, `ZONES`,
`VELOCITY` and `SCENE_STABILITY` are ungranted, which means `intrusion` reports UNAVAILABLE with
the missing capability named — the capability system working as designed, not a defect. No
incident is created by anything, in any phase so far: P4 owns that.

## What does not exist

| Area | State | Planned |
|------|-------|---------|
| Tracker association and lifecycle | A Kalman box filter exists (`tracking/kalman.py`). Detection-to-track association, track lifecycle, epoch reset and velocity estimation do not. `TRACKING` and `VELOCITY` stay ungranted | P2 |
| Scene engine | Zone *configuration* loads and validates (`config/zones.py`). The analyzer that turns tracks plus zones into a `SceneState` — occupancy, enter/exit diffing, spatial relations, the stability signal — does not exist, so `ZONES` and `SCENE_STABILITY` stay ungranted | P2 |
| Event chain wired to live cameras | Built and tested from `SceneState` onward, but nothing produces a `SceneState` from frames, so the chain does not run on a live camera. Driven by scripted scenes in tests | P2, then wiring |
| Arbitration: merge and spatial coalesce | The **subsume** and **escalate** rules of 06 §7 are implemented. **Merge** needs `FIRE`/`SMOKE` and **spatial coalesce** needs `region:` subjects; neither event type has a module, so both rules would be written against events that cannot occur. `arbitration.merge_window_ms` exists as a key | P6 |
| Sequence patterns in a shipped module | `temporal/sequence.py` implements the ordered-pattern matcher of 04 §6.4 and is unit-tested, but no shipped module uses it yet | P5 (`abandoned_object`) |
| Candidate persistence | `CandidateRecord` is produced for every verified, rejected and expired candidate, including the accumulator trace. Nothing writes it to `candidate_events`; it is returned from the analysis tick and discarded | P4 |
| Rejection feed in the API and console | Records exist in memory per tick. No API route and no console view consume them | P4 |
| Threshold tuning against real footage | **Not done.** Every value in `temporal`, `verification` and `severity` is the architecture's starting default. Tuning needs a live pipeline and 30 minutes of real footage, which needs P2 | after P2 |
| Incidents, operator actions | `IncidentStatus`, transitions and `Incident` exist as domain types. Nothing constructs an incident, and no operator action is implemented. Mutes are honoured by a validator but there is no way to create one | P4 |
| Alert channels | Protocol only | after P4 |
| Web operations console | Not built. A **provisional viewer** at `/` shows the live streams and metrics; it is not the console | later |
| RTSP / IP cameras | Config validates (and rejects inline credentials). Such a camera is reported **FAILED: not implemented** | later |
| Video-file replay, image input | Not built | later |
| Ultralytics backend | Deliberately not built (AGPL, drags in PyTorch); see ADR 0012 | only if needed |
| Degradation ladder, supervisor/restart | Not built. The scheduler bounds load, but nothing lowers resolution or demotes cameras under overload, and a crashed worker is reported, not restarted | later |
| Windows Credential Manager (`keyring`) | Not implemented; secrets come from environment variables or `.env` | when needed |
| Ground-plane calibration, m/s velocity | Not built | P5 |

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
- **Real-world false-positive rate is unknowable from here.** It needs real footage and, from P4,
  the measured dismissal rate.
