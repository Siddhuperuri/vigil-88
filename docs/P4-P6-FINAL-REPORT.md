# VIGIL-88 — final report for P4, P5 and P6

**Date:** 2026-10-21 · **Branch:** `feat/p2-scene-engine-p4-persistence` · **Gate:** `ALL GATES
PASSED` (1,916 tests, 2 skipped, 18 coverage floors, 3/3 import contracts, both generated
documents current, `vigil doctor` and `vigil run --selftest` clean).

The twelve sections the brief requires. Nothing here is a claim that is not either measured or
pointed at the test that holds it, and §8 to §12 are the ones that matter most.

---

## 1. What was implemented and verified

**P4 — incidents and evidence.** SQLite persistence with WAL and migrations; the incident
lifecycle state machine of 06 §9 with operator actions and a full transition history; evidence
capture with JPEG-encoded pre-roll and post-roll, snapshots, scene/track/explanation dumps, a
manifest and SHA-256 over every file; a retention worker; console, JSONL and webhook alert
channels each with an honest `is_available()`; incident, evidence and mute API routers.

**P5 — modules and analytics.** `person_down` and `abandoned_object`, both added without touching
anything outside their own directories; four-point ground-plane calibration granting
`GROUND_PLANE` and filling `velocity_m_s` at the foot point; an analytics layer of `COUNT`/`SUM`/
`GROUP BY` aggregations over persisted rows with JSON, CSV and (behind the `reports` extra) PDF
export; a provisional viewer showing streams, metrics, the incident list, an incident's full
explanation and evidence, operator actions and mute countdowns.

**P6 — perception and hardening.** Covered in §2, because almost all of it was change rather than
addition. The one piece of new surface is `events/builtin/fire/` and `smoke/`, which are
**declarations that the capability is absent**, not detectors.

**Verified end to end**, not in pieces: `tests/integration/test_incident_workflow.py` drives a
real `Application` on real threads against a real SQLite file and asserts that one sustained
intrusion produces **exactly one** incident, that its evidence is hashed and self-describing, and
that both survive a restart with `PRAGMA integrity_check` returning `ok`.

## 2. What was changed and why

P6's substance is that **auditing the running system against its own design found ten defects that
the unit suite could not see**, because every one is a property of how two components behave
together over time. ADR 0015 §B has each in full.

| # | What was wrong | Why it mattered |
|---|---|---|
| 1 | Camera health compared the measured frame rate against the driver's **claim** | Below `min_fps_ratio` of the claim a camera is DEGRADED, and DEGRADED makes verification reject **every** candidate. A webcam advertising 30 fps and delivering 14 — which the development webcam does — therefore **detected nothing at all**, for its whole life, with `low fps` as the only symptom |
| 2 | `_read_failed` cleared the attempt counter, and so did every successful open | A camera that opened then failed its read retried at the 500 ms base delay **for ever**, hammering the device, and never reached FAILED |
| 3 | Worker heartbeats were maintained and never read | A hung worker reported `alive, not crashed` and application health stayed **OK**. `pipeline/health.py` contained a `for` loop whose entire body was `continue`, left where the check belonged |
| 4 | The first version of that check false-positived on the retention worker | It sleeps an hour by design. Fixed by bounding every worker's beat cadence (`core/heartbeat.py`) rather than tuning a per-worker threshold |
| 5 | `SpillQueue._spilled += 1` ran outside the lock | Lost counts under the concurrent overload the counter exists to measure |
| 6 | A mid-stream format change passed `expected_fps=None` | Silently disabled the low-fps check for the rest of the stream |
| 7 | An epoch reset discarded **all** accumulators | 07 §5 requires `track:` discarded and `zone:`/`region:`/`camera:` **decayed across the gap** — "a fire does not stop burning because the network blipped". Invisible today because all three shipped modules use `track:` subjects, and silently wrong for the first one that would not |
| 8 | Arbitration computed `suppressed_by` and **only logged it** | 06 §7's subsume rule suppressed nothing, so a `RESTRICTED_AREA_ENTRY` inside an active `INTRUSION` would have produced the two incidents for one situation the rule exists to prevent — and `LIMITATIONS.md` called it implemented |
| 9 | The inference→analysis edge dropped frames for **every** camera | 07 §2 makes losslessness the property that *defines* a PACED source. `vigil run --selftest` had been silently losing **27 of its 60 frames** |
| 10 | `record_dropped()` was called from no production code, and `vigil_frames_dropped_total` was never incremented | `frames_dropped` was structurally **zero** in every snapshot and every persisted metric. This is what hid #9 — wiring the counter revealed it within a minute |

Two further changes were structural rather than corrective: `pipeline/scoring.py` was extracted
from `analysis.py` (which had reached the 500-line ceiling) and is now the single place a
suppression takes effect; and `core/capabilities.py` gained `UNGRANTABLE`, which turns "nothing in
this build can supply this" into a machine-checked fact rather than a comment.

## 3. What deviates from the architecture

| Deviation | Why |
|---|---|
| **No fire or smoke detector**, where 10-roadmap §9 planned a tile classifier | No model with a compatible licence, checkable provenance and a published held-out evaluation could be sourced. §9 permits exactly this outcome as its second branch. ADR 0015 §A |
| **Merge compares subject equality**, where 06 §7 says "overlapping regions" | The subject is the only mechanical reading of overlap available, because it is what the accumulator was keyed by. Adjacent cells are *spatial coalesce*'s job, which is not built. Inferring overlap from `zone_ids` would merge a fire at one end of a yard with steam at the other, and a fabricated merge is worse than two incidents |
| **Spatial coalesce is not implemented** | It needs `region:` subjects and no module emits them |
| **The watchdog does not force-close a stalled source**, which 07 §5 specifies | Releasing an OpenCV capture from another thread is not safe (ADR 0011). A stall is detected and the camera marked DEGRADED; since P6 the stalled *worker* is visible in application health too |
| **No pre-roll gap annotation and no `stream_interrupted` on open incidents** (07 §5 rows 4 and 5) | Not built. An operator reviewing a clip spanning a reconnect is not told frames are missing from it |
| **`min_duration_ms` is measured over the evidence span**, not "since the first observation" (04 §6.3) | Measured literally to *now*, the threshold becomes satisfiable by waiting, and 06 §14 requires the opposite. Caught by a test, recorded in LIMITATIONS since P3 |
| **No Ultralytics backend**, which 03 planned for development velocity | AGPL-3.0 plus 2.5 GB of PyTorch for no capability the ONNX path lacks (ADR 0012) |
| **No degradation ladder and no worker restart** (01 §9) | Not built. Load is bounded by the scheduler only |

06 §12's worked example also disagrees with its own arithmetic in two places; the factor table is
normative and is what `severity/` implements. Recorded in LIMITATIONS since P3.

## 4. What is tested, and how

1,916 tests. Five distinct kinds, deliberately kept apart because they prove different things:

- **Property tests** over randomised inputs for the accumulator: a `0.5` signal contributes
  exactly zero, `L` stays inside `[-l_bound, +l_bound]`, stronger evidence never lowers `L`, decay
  halves `L` over one half-life to within 1e-9, an identical sequence yields an identical state.
- **A parameterised module contract suite**, now in two halves split by a property of the source
  (`ungrantable(requires)`), not a list of names. Modules that observe get purity, signal-range,
  bounded-state, time-budget and *declared-capability-use* checks — that last one removes each
  declared capability in turn and asserts the behaviour changes, which catches both over- and
  under-declaration. Modules that only declare get a different contract: UNAVAILABLE with a reason
  naming the capability, `observe()` raising rather than reporting silence, no parameters, no
  shipped version, and a README that says it is not implemented.
- **Integration scenarios on a real `Application`**: one sustained intrusion → exactly one
  incident; a 300 ms boundary crossing → none; sixty seconds of oscillation → at most one confirm
  and one resolve; an unstable scene, an unhealthy camera and an operator mute each → zero events
  with a recorded reason; survival across restart.
- **Failure-path tests in isolation**: a full disk, an unwritable directory, an encoder producing
  nothing, a write interrupted mid-flight, a tampered evidence path, a hash mismatch on read, a
  retention sweep that cannot delete. Each produces a *named failure*; evidence is never
  fabricated.
- **Repository gates** no linter expresses: no real-time reads outside `core/clock.py`, no
  swallowed broad exceptions, no machine-specific absolute paths, no credential-shaped literals
  (both now covering `docs/` as well as code), no source file over 500 lines, plus `import-linter`
  layer, engine-independence and domain-purity contracts.

P6 added 60-odd tests specifically for §2's defects, each asserting the *consequence* rather than
the line: that a camera whose driver overstates its rate recovers and can detect again; that a
flapping camera's backoff grows and it eventually reaches FAILED; that a stalled worker fails the
system and a finished one does not; that a kept accumulator decays across a gap through the
production path; that a PACED handoff never drops and is released by `close()`; that a suppressed
event produces no incident and its record says so.

Coverage floors are highest where a bug is most expensive: 95% for `temporal/`, `verification/`,
`tracking/` and `analytics/`, 90% for the other decision engines.

## 5. What is not tested

- **Anything requiring real footage of the thing being detected.** There is no fall dataset, no
  abandoned-luggage dataset and no fire/smoke dataset here. The unit tests prove arithmetic,
  thresholds, degradation paths and false-positive *mechanisms*; they prove nothing about how
  often a module fires correctly in a real place.
- **Long-run behaviour.** The longest sustained run in this repository is **120 seconds**. P4's
  8-hour synthetic soak and P6's 24-hour real-camera soak were **not run**. The evidence ring's
  byte ceiling is asserted over seconds, not hours.
- **Failure drills.** P6's exit criteria list unplug, disk full, model deleted, config corrupted
  and GPU driver reset as drills to execute and document. **None was executed.** The equivalent
  code paths have deterministic unit tests, which is not the same as having survived them.
- **Hardware other than one machine.** The real-webcam test is opt-in (`pytest --webcam`). One
  GPU, one webcam, one OS.
- **RTSP, video-file replay and image input**, which are not implemented.
- **The merge arbitration rule in production.** It is unit-tested and has no producer in this
  build, because fire and smoke ship as declarations.
- **Calibrated confidence.** No reliability diagram has been plotted against labelled outcomes.

## 6. Measured performance

From `docs/PERFORMANCE.md`, which is generated from stored results and is the only place in the
repository permitted to make a performance claim. Five new sustained 120 s runs (YOLOX-S on CUDA,
640×480 synthetic sources at 15 fps, 10 fps inference target per camera, execution provider
verified per run, first-30 s against last-30 s thermal delta reported for each).

| cameras | demanded | delivered | inference p50 / p95 | capture→detections p50 | skipped | dropped | mean SM clock |
|---|---|---|---|---|---|---|---|
| 1 | 10 fps | **10.0** | 36.5 / 42.0 ms | 79.3 ms | 599 | 0 | 591 MHz |
| 2 | 20 fps | **20.0** | 22.9 / 28.1 ms | 86.7 ms | 1,198 | 0 | 1,086 MHz |
| 4 | 40 fps | **40.0** | 19.7 / 24.4 ms | 126.5 ms | 2,396 | 0 | 1,301 MHz |
| 6 | 60 fps | 52.1 | 18.6 / 22.1 ms | 110.9 ms | 4,546 | 0 | 1,376 MHz |
| 8 | 80 fps | 51.6 | 18.8 / 22.4 ms | 110.0 ms | 8,211 | 0 | 1,455 MHz |

- **The measured ceiling is about 52 inferences per second.** Four cameras are the most that are
  **served in full** at 10 fps each. Six and eight keep working and shed load by skipping, which
  is designed behaviour and is counted — but the per-camera rate is no longer what was configured.
- **One camera has the *worst* inference latency of the five.** A laptop GPU idles its clocks at
  low duty cycle: 591 MHz at one camera against 1,455 MHz at eight. This confirms ADR 0012 A7 on
  new data and is why sparse runs are published beside saturated ones.
- No run was thermally throttled; peak GPU temperature was 66 °C.
- Earlier P1 figures remain in the same file: 30 fps single-camera at p50 18.0 ms, a saturation
  run reaching 58.2 fps with 25 s of reported thermal slowdown, and CPU-only at 13.3 fps.

**These are throughput numbers and nothing else.** The synthetic sources are test patterns with
nothing for a detector to find, so their post-processing cost is that of an empty scene. No number
here says anything about detection quality.

## 7. Security posture

Each constraint was checked against the code rather than asserted. ADR 0015 §C has the detail.

**Verified as claimed.** Parameterised queries throughout — every SQL interpolation is a literal
in its own module, a run of `?` placeholders counted from `len(values)`, or a sort column from an
allowlist that raises on an unknown key. No path traversal: `safe_join` enforces both a strict
per-component pattern **and** containment after symlink resolution, and analytics exports use the
same function. No credentials or machine-specific paths in source, config, docs or the committed
benchmark results. The API binds loopback by default and the config validator refuses any other
bind without `auth.enabled` and a ≥32-character token, compared with `hmac.compare_digest`.
Secrets are rejected in config with the env-var form named, and `hide_input_in_errors` keeps a
rejected password out of Pydantic's own error text.

**Two gaps found and closed.** The ruff S608 exemption covered `analytics/` as well as
`persistence/`, and only the latter had a test paying for it; `analytics/` now has one, including
an **AST check that every fragment interpolated into SQL at every call site is a literal** — the
check that survives someone threading an API query parameter through next year, which is the only
way that exemption could become unsafe. And "no full image frames in logs" held **only because no
caller had ever logged one**: redaction now summarises any bytes-like value over 256 bytes and
anything array-like, so it holds by construction. The path and secret gates were also extended to
`docs/`, because `PERFORMANCE.md` and the committed benchmark results are generated from machine
data and are the likeliest route for a home directory to reach the repository.

**Two findings left open, deliberately.** The `onnx-gpu` extra pulls five NVIDIA CUDA
redistributables under the proprietary CUDA EULA — permitted under its redistribution clause, but
the stack is not wholly Apache/BSD and should not be described as if it were. And **this project
declares no licence of its own**: no `license` field, no `LICENSE` file, so by default no rights
are granted to anyone. Choosing one is the owner's decision, not an implementation phase's.

Otherwise: no AGPL or GPL dependency anywhere, `reportlab` is BSD, all inference is local, and
there is no outbound network except explicitly configured alert channels.

## 8. What remains unimplemented

Fire and smoke detection (declared, not built). Spatial coalesce. The degradation ladder.
Automatic worker restart. Pre-roll gap annotation and `stream_interrupted` on open incidents.
Force-closing a blocked read. The seven-view operations console — what exists is a provisional
viewer that follows the honesty rules. RTSP and IP cameras (config validates; such a camera is
reported FAILED: not implemented). Video-file replay and image input. Pose estimation. Cross-camera
identity. Multi-user auth and RBAC. Email, SMS and MQTT channels. WebRTC. Docker packaging. INT8
quantisation. NVDEC hardware decode. `keyring` secret storage. A single-file evidence bundle.
Event modules for `vehicle_collision`, `crowd_anomaly`, `dangerous_motion` and `vehicle_anomaly`.

`docs/LIMITATIONS.md` is the full inventory and states, for each, the interface and the reason.

## 9. Real-world validation limitations

This is the section that matters most, and it has not improved across P4, P5 or P6.

1. **There is no measured false-positive or false-negative rate for any module, and none can be
   computed here.** That needs labelled ground truth, which does not exist in this repository.
2. **There is no mAP, precision or recall for the detector.** Published YOLOX weights are consumed
   as-is. The only accuracy evidence is qualitative: annotation was checked against one real
   webcam scene and a person was boxed correctly.
3. **No threshold anywhere has been tuned against real footage.** Every value in `temporal/`,
   `verification/`, `severity/` and every module's `params.py` is the architecture's starting
   default. The tuning observations in LIMITATIONS — that confidence saturates within ~1.5 s at
   the shipped defaults, that boundary oscillation is suppressed by `min_support_ratio` rather
   than by hysteresis, that the intrusion dwell bonus is a stronger knob than its size suggests —
   are measurements of the *mathematics*, not of a scene.
4. **A confidence is a score, not a probability.** It orders events sensibly; "0.8" does not mean
   "right four times in five". Analytics never aggregates confidences into a rate.
5. **The dismissal rate is not a false-positive rate.** A dismissal can be correct detection of
   something the operator did not care about, and a missed event is invisible to it. It is
   recorded per module version and has no data behind it yet.
6. **`person_down` is bounding-box geometry and makes no medical claim.** The dominant false
   positive is someone sitting or lying down deliberately, which is geometrically identical to a
   collapse.
7. **`abandoned_object` cannot see a bag dropped before its track existed** — before startup,
   before a reconnect, before re-acquisition. The same rule that keeps it silent about furniture
   causes this.
8. **Calibrated velocity is only as accurate as the tape measure**, nothing off the plane is
   right, and nothing can say so: a projective map of a plane answers confidently and wrongly for
   a staircase, a ramp or a thrown object.
9. **The P5 criterion "within 10% of a tape-measured ground truth" is unmet and unmeetable here.**
   It needs a physical camera, a tape and someone walking a measured distance. What is proven is
   the mathematics against synthetic geometry with known answers.

## 10. Datasets and hardware: what was and was not used

**Datasets: none.** No dataset was downloaded, used, or shipped, for training or for evaluation.
All event-logic testing is synthetic — a scene generator, a `MockDetector`, `SceneStateBuilder`
fixtures and planted boxes with known answers. The detector's weights are published YOLOX
(Apache-2.0) exports fetched from a fixed registry of HTTPS URLs with the SHA-256 recorded on
first fetch and verified on every load; upstream publishes no checksums, so that first fetch is
trust-on-first-use, which is stated rather than hidden. Candidate fire/smoke datasets (D-Fire,
FiSmo, BoWFire, FIRE-SMOKE-DATASET) were **noted and not downloaded**; each would need its own
licence review.

**Hardware used:** one machine — Windows 11, Intel 16-logical-core CPU, 15.6 GB RAM, **RTX 3050
Laptop GPU with 6 GB VRAM on AC power**, with a normal desktop's other applications running;
ONNX Runtime 1.26 on CUDA 12 from pip wheels; one USB webcam which, on this machine, cannot be
opened through MSMF and falls back to DirectShow.

**Hardware not used:** no desktop GPU, no second GPU, no NVDEC evaluation, no IP or RTSP camera,
no second webcam, no non-Windows platform, no battery-powered run. Everything in
`PERFORMANCE.md` describes that one machine and will change with different power settings,
thermals or background load.

## 11. The deployment story

`uv sync --group dev --extra onnx-gpu --extra api` in a checkout, or `pip install` of the wheel
plus the extras a deployment needs. `uv build` produces both an sdist and an installable wheel
carrying the console script and all five module READMEs.

Extras exist so that no deployment pays for what it does not use: `capture` (numpy, OpenCV),
`onnx-cpu` / `onnx-gpu` (conflicting, because both provide the same module), `api`, `reports`
(reportlab, for PDF). **A bare install now starts and degrades honestly** — until P6 it did not,
because a module-level `import cv2` eight modules down the CLI import chain killed even
`vigil doctor`, the one command an operator runs *because* something is missing. Verified in a
clean virtual environment: `vigil doctor` runs with core dependencies only and reports every
capability as absent with an actionable reason.

One process, one operator, loopback by default. A non-loopback bind requires a bearer token and
the validator refuses otherwise; note that with auth enabled a browser `<img>` cannot send
headers, so the MJPEG stream works for programmatic clients but not for the provisional viewer's
bare tag — the session flow that fixes this belongs to the full console. There is no Docker image
(10-roadmap §10 declines it for GPU-passthrough friction on Windows), no installer, no service
wrapper, no multi-user story, and no upgrade/migration procedure beyond Alembic's.

`docs/OPERATIONS.md` holds the operating notes. `docs/CONFIG.md` is the generated configuration
reference and is regenerated by the gate, so it cannot drift.

## 12. What production readiness does and does not mean here

**It launches. That is not the same thing, and this report does not claim otherwise.**

What has been earned is *engineering* confidence in the machinery. The chain runs end to end on
real threads against a real database; incidents are durable, evidenced, hashed and explainable as
arithmetic; failure paths produce named failures rather than placeholders; the quality gate is
clean on 1,916 tests with coverage floors highest where a bug would be most expensive; the layer
and purity contracts are machine-checked; and P6 removed ten real defects, one of which meant a
common class of webcam **detected nothing at all** while reporting itself merely DEGRADED.

What has **not** been earned is any confidence about detection:

- **No accuracy figure exists for anything.** No false-positive rate, no false-negative rate, no
  mAP, no precision, no recall — for any module or for the detector. Not "low"; *unknown*.
- **No threshold has been tuned against reality.** Every one is a starting default.
- **Nothing has run longer than two minutes.** Neither soak was executed, and no failure drill was
  performed. Memory, handle counts and reconnect recovery over hours are untested claims.
- **One machine, one camera, one OS.** Behaviour on other hardware is unverified.
- **Fire and smoke do not work**, and say so through the system rather than only in a document.
- **The system has no declared licence**, which is a deployment blocker before it is a technical
  one.

A fair summary: **this is a well-engineered platform whose perception has never been evaluated.**
Putting it in front of real operators would be an experiment, not a deployment, and the first
honest output of that experiment would be the dismissal rate — the one instrument that is already
built and has no data behind it. Anyone proposing to rely on it should first collect footage from
the actual scene, label it, measure what it does, and tune from that measurement. Until then the
only safe reading of an incident from this system is "something here is worth a human look", which
is a useful thing for it to say and a much smaller claim than "an incident occurred".
