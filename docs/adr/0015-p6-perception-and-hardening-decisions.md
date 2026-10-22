# 0015 — P6 perception and hardening decisions

- **Status:** accepted
- **Date:** 2026-10-21
- **Phase:** P6

Continues [0014](0014-p5-module-and-analytics-decisions.md). The design set remains the source of
truth; this records where P6 chose, deviated, or found that the implementation did not match what
the documents said it did.

P6's headline is not a feature. It is that **auditing the running system against its own design
found nine defects that no unit test could see**, because every one of them is a property of how
two components behave together over time. They are in §B, in the order the tests assert them.

---

## A. Fire and smoke: no usable model, so no detector

### A1. The decision

`10-roadmap` §9 allows exactly two outcomes: fire/smoke "evaluated on a held-out set with
measured precision and recall, recorded — **or** the module is not shipped and `LIMITATIONS.md`
says why. There is no third option."

**This is the second branch.** No fire or smoke detection is shipped. What ships is a
*declaration* that the capability is absent, through the mechanism the architecture already has
for exactly this (D-008 capability negotiation), so that the gap is **reported by the running
system** rather than inferred by a reader from a missing directory.

### A2. The survey, and why every candidate failed

Searched the Hugging Face Hub (22 model repositories matching "fire smoke detection", by
downloads) and the open web, on 2026-10-21, against three tests: a licence compatible with this
project, checkable provenance, and usability on the ONNX Runtime path inside a 6 GB VRAM budget
that already holds YOLOX-S.

| Candidate | Licence | Form | Why it fails |
|---|---|---|---|
| `xiazh0219/yolo26m-UAV-fire-smoke-detection`, `sanqierere/yolo26m-UAV-…`, `Farhatullah0066/yolov8n-…` | AGPL-3.0 | Ultralytics `.pt` | AGPL-3.0, which [0012](0012-p1-vision-decisions.md) A1 refused for this system: the network clause is triggered by serving a UI, which is what this does |
| `Beehzod/yolov26-fire-smoke-detection` | CC-BY-4.0 | Ultralytics | A CC-BY grant over weights produced with Ultralytics code is a grant the uploader may not hold. CC licences are also not written for software or weights |
| `aryantyagiase/yolov8-fire-smoke-detection` | **none declared** | ultralytics + onnx | No licence is not permission. The highest-download result, and unusable |
| `TommyNgx/YOLOv10-Fire-and-Smoke-Detection` | apache-2.0 *declared*, `base_model: Ultralytics/YOLO11` | PyTorch safetensors, **gated** | Self-contradictory: an Apache-2.0 grant over a fine-tune of a model whose publisher asserts AGPL-3.0 reaches fine-tuned weights. Also gated, so there is no fixed public HTTPS URL, which breaks [0012](0012-p1-vision-decisions.md) A4 |
| `Intel/fire-and-smoke-detection` | MIT | OpenVINO Qwen2-VL | A ~2B-parameter vision-language model for OpenVINO. Not an ORT detector, will not fit beside YOLOX in 6 GB, and per-frame VLM inference is orders of magnitude off 20 fps |
| `prithivMLmods/Fire-Detection-Engine-ONNX` | apache-2.0 | ViT-base ONNX, 224×224, 3 classes | The only genuinely permissive ONNX candidate, and still not usable — see A3 |
| `shawnmichael/*` (7 repos) | apache-2.0 | safetensors | Bare `generated_from_trainer` cards: no dataset named, no metrics, no prose. Zero provenance, and not ONNX |

### A3. Why the one permissive ONNX candidate was still refused

`prithivMLmods/Fire-Detection-Engine-ONNX` is Apache-2.0 and genuinely ONNX. It was refused on
four independent grounds, any one of which would be sufficient:

1. **It cannot localise.** Its labels are scene categories — `Fire Needed Action`,
   `Normal Conditions`, `Smoky Environment` — over the whole image. It cannot populate a
   `region:` subject, which is what `06` §3 and §7 (merge, spatial coalesce) are built around.
2. **Its training data has no provenance.** The card names no dataset, no size and no dataset
   licence, only "a curated dataset of fire place conditions". An Apache-2.0 grant on weights
   says nothing about rights in the images they were trained on.
3. **Its metrics cannot be cited.** The card reports per-class precision 0.97–0.98 over 2,424
   samples and never states that this is a held-out split, never names it and never describes
   it. The exit criterion is a *measured* precision and recall; an undocumented number on a model
   card is not a measurement. The card also contradicts itself, saying "binary classification"
   while declaring three labels.
4. **We could not measure it ourselves.** There is no labelled fire or smoke data in this
   repository, which is the same gap that blocks every other module's accuracy claim.

### A4. Why not a colour or flicker heuristic

Fire is orange and flickers, so a hue-plus-flicker score is an afternoon's work. It would fire on
sunsets, brake lights, hi-vis jackets, sodium street lighting, heaters, televisions and the
reflection of any of them in wet ground — and those are not a tuning problem, because the
heuristic has no representation of what distinguishes them. Shipping it would be a control that
appears to work, which is the single thing this architecture exists to prevent (README rule 1). A
detector with an untrustworthy alarm rate is worse than no detector, because it also consumes the
attention the real alarms need. The false positives a real implementation must beat are tabulated
in each module's README so the next person starts from the problem.

### A5. `UNGRANTABLE`: making "this build cannot grant it" a machine-checked fact

`core/capabilities.py` gains `UNGRANTABLE`, naming the capabilities **no component in this build
can supply whatever the configuration says**, each with a reason: `classification` (this ADR),
`segmentation`, `pose`, `optical_flow`.

This is a stronger statement than "not currently granted", and the difference matters to an
operator: a missing `zones` capability closes by writing a zone file, while nothing they can do
closes these. It is declared rather than commented so that three things become mechanical — the
module API and `vigil doctor` can say *this build cannot grant it*; the contract suite can tell a
module that observes from one that only declares; and a declaration-only module cannot drift into
looking operational.

### A6. The modules are declarations, and `observe()` raises

`events/builtin/fire/` and `events/builtin/smoke/` each export a descriptor requiring
`Capability.CLASSIFICATION`, version **`0.0.0`**, and **no parameters**. Both choices are
deliberate: a `1.x` would make the dismissal-rate-per-module-version metric attribute operator
judgements to an algorithm that never ran, and a `params.py` of plausible-looking thresholds is
the first step towards a module that looks implemented.

`observe()` **raises `CapabilityError`** rather than returning an empty sequence. The two are not
equivalent: an empty sequence is indistinguishable from "no fire in this frame", which is a claim
neither module has any means to make. The capability gate means it is never called; `bind()` logs
a warning, because if it is ever bound something upstream has gone wrong.

Smoke gets its own module rather than being folded into fire, for two reasons: `06` §7's merge
rule is defined over two distinct event types, and the two differ in what shipping them would
need. **Smoke is the harder one.** Fire has an appearance; smoke is defined by what it does to the
scene behind it, which is invisible in a single frame. A fire classifier can be trained and
evaluated on stills, and the public datasets are overwhelmingly stills; a smoke detector worth
trusting needs labelled **video**. The dataset gap for smoke is not "no labelled data" but "the
labelled data that exists is the wrong kind".

### A7. The contract suite splits in two, by a property of the source

A module that can never run here has nothing for the behavioural contract — purity, signal
ranges, bounded state, declared-capability use — to measure. So
`tests/contract/test_module_contract.py` now parameterises `TestDetectionModuleContract` over
modules that could run and adds `TestDeclarationOnlyModuleContract` for those that cannot.

The split is computed from `ungrantable(descriptor.requires)`, **not from a list of names**, so a
module of either kind lands in the right half with nothing to maintain, and
`test_every_module_is_in_exactly_one_half` holds it closed. The declaration contract is not a
weaker contract but a different one: it asserts that the registry reports UNAVAILABLE with a
reason naming the capability, that `observe()` raises rather than reporting nothing, that there
are no parameters and no shipped version, and that the README says plainly that it is not
implemented.

### A8. `06` §7 merge, which had been waiting on FIRE/SMOKE

`arbitration.py`'s P3 docstring deferred **merge** and **spatial coalesce** because neither
`FIRE`/`SMOKE` nor `region:` subjects existed, and wiring them would have meant "writing rules
against events that cannot occur". P6 implements **merge** anyway, and the reason it is not the
same mistake is that the rule is pure logic over two verified events and is reachable by a
third-party module through the `vigil88.modules` entry-point group. What it lacks is a
*production* exercise, which `LIMITATIONS.md` states.

- `MERGED_INTO = {SMOKE: FIRE}` is **one-directional**, and a test asserts the table has no cycle:
  a symmetric table would let two events suppress each other and report neither.
- **Overlap is subject equality.** `06` §7 says "overlapping regions", and the subject is the only
  mechanical reading available, because it is what the accumulator was keyed by. *Adjacent* cells
  are the **spatial coalesce** rule's job, which is not built — so without it, merge joins events
  on the identical subject and nothing wider. Inferring overlap from `zone_ids` across different
  subject kinds would merge a fire at one end of a yard with steam at the other, and a fabricated
  merge is worse than two incidents.
- Subsumption wins over merge, so one event carries one reason rather than two contradictory ones.

**Spatial coalesce remains unimplemented** and is recorded in `LIMITATIONS.md`.

---

## B. Nine defects found by auditing the running system

Each is a property of how components behave together over time, which is why the unit suite was
green throughout. Listed in the order `tests/unit/ingest/test_reliability.py` and its siblings
assert them.

### B1. A driver that overstated its frame rate silenced the camera completely

`ingest/health.py` compared the measured rate against `SourceInfo.fps`, which is the driver's
*claim* (`CAP_PROP_FPS`). Below `health.min_fps_ratio` of the claim the camera is DEGRADED —
and `verification/validators/chain.py` lists DEGRADED in `UNHEALTHY_STATES`, so validator 1
**rejects every candidate**. A webcam that advertises 30 fps and delivers 14, which is what the
development webcam does in ordinary indoor light, therefore detected **nothing, for its entire
life**, with `low fps` as the only symptom. `LIMITATIONS.md` had recorded half of this ("the
tracker should compare against the measured rate") without recording the consequence.

`07` §5's justification for suppressing on DEGRADED is *irregular frame spacing*, which makes
temporal reasoning unsound. A steady 14 fps is not irregular: the accumulator decays by frame
timestamps and does not care what the driver promised. So the expectation now starts at the claim
and is **replaced, once per stream epoch, by what the camera has actually sustained** after
`health.adopt_measured_fps_after_frames`, with `health.min_absolute_fps` as a floor that no amount
of demonstration lowers. Adoption is once per epoch rather than continuous, because a continuously
re-adopted expectation would track a dying camera all the way down and never report anything. The
expectation is never raised above the claim, and the DEGRADED detail names the claim it replaced.

### B2. Reconnect backoff never escalated and the attempt limit was unreachable

`capture_worker._read_failed` set `_open_attempts = 0` and waited `_backoff(0)`; the following
`_open()` succeeded and set it to 0 again. A camera that opens and then fails its first read
therefore retried at the ~500 ms base delay **for ever**, hammering the device, and never reached
FAILED — `_gave_up` was unreachable on that path, so an operator was never told it had given up.

Fixed by counting reconnect **cycles** separately from consecutive open failures. A cycle clears
only once the reopened stream has delivered `ingest.reconnect_reset_frames` frames, because an
open followed by a failing read has not recovered anything. A camera that genuinely recovers and
runs is not punished for an old outage.

### B3. Worker heartbeats were recorded and never read

`ThreadWorker` has maintained `heartbeat_age_ms` since P0 and `aggregate_health` never consulted
it, so a worker that was alive but no longer progressing — the blocked `read()` of
[0011](0011-p0-implementation-deviations.md), a deadlock, a disk that stopped answering —
reported `alive=True, crashed=False` and the application's health stayed **OK**. A crash was at
least visible. `pipeline/health.py` even contained a `for w in workers:` loop whose entire body
was `continue`, left where the check was meant to go.

`stalled_workers()` now reports any worker whose beat is older than
`pipeline.worker_stall_timeout_ms` (default 30 s), naming each one, and the level is **FAILED**
for the same reason a crash is: part of the pipeline has stopped doing its job and nothing in
this build will restart it.

### B4. The stall check's first version was wrong, and the benchmark caught it

Switched on, it reported the **retention worker** stalled 30 s into every benchmark run, and the
benchmark correctly aborted. The worker was not stalled: `retention.sweep_interval_s` is 3600 s by
default, so it legitimately beats once an hour.

The fix is not a per-worker threshold, which would make the budget a tuning exercise and would
have to be kept in step with every worker's interval. It is to make the beacon mean what the check
needs it to mean — *this thread is responsive* — by beating while waiting. `core/heartbeat.py`'s
`wait_beating` serves a long wait in slices with a beat between them, so one stall budget is
correct for every worker. This is recorded because it is the kind of mistake worth not repeating:
a liveness beacon is only interpretable if every holder's normal cadence is bounded.

### B5. `SpillQueue` lost counts under the overload it measures

`_spilled += 1` ran outside the lock. A read-modify-write with several analysis threads spilling
concurrently undercounts, which is the one condition the counter exists for. Moved inside.

### B6. A mid-stream format change disabled the low-fps check

`capture_worker._on_format_change` re-marked the camera ONLINE with `expected_fps=None`,
discarding the known rate, so the low-fps check was silently off for the rest of the stream.
`mark_online` now treats `None` as "keep what is known".

### B7. An epoch reset discarded accumulators it was supposed to keep

`07` §5's table is explicit, and calls this "the subtle one, and it is the kind of detail that
decides whether a system is trustworthy across real-world network conditions": `track:`
accumulators are **discarded** because the identity is gone, while `zone:`, `region:` and
`camera:` accumulators are **decayed across the gap, not discarded** — "a fire does not stop
burning because the network blipped".

`analysis.py::_on_epoch_reset` called `store.reset()`, which cleared all of them. No shipped
module could notice, because `intrusion`, `person_down` and `abandoned_object` all use `track:`
subjects — and it would have been silently wrong for the first module that did not, which is
precisely the kind of latent error a plugin architecture must not have. `AccumulatorStore.
reset_tracks()` now drops only the identities. The decay needs no extra work: accumulators decay
by frame timestamps, so the first observation after the gap applies every elapsed half-life by
itself, which a test asserts through the production `decay_all_to` path.

### B8. Arbitration computed suppressions that nothing acted on

`ArbitrationOutcome.suppressed_by` has been computed since P3 and was **only ever written to a log
line**. `response_worker.handle` created an incident for every scored event regardless. So `06`
§7's **subsume** rule suppressed nothing: a `RESTRICTED_AREA_ENTRY` inside an active `INTRUSION`
for the same track would have produced the two incidents for one situation that the rule exists to
prevent — and `LIMITATIONS.md` described subsume as implemented.

New `pipeline/scoring.py` is the one place a suppression takes effect. A suppressed event is kept
out of `scored`, which is the response stage's only input, so no incident, evidence capture or
alert follows; its `CandidateRecord` is rewritten from VERIFIED to **`CandidateOutcome.SUPPRESSED`**
(a value `05` §5 had defined for exactly this and nothing used) carrying arbitration's reason, so
the candidate table cannot claim a verification the incident table cannot account for; and its
subject is reported back so the ongoing-extension pass does not try to grow an incident that was
never created.

The module was extracted rather than added in place because `analysis.py` had reached the
repository's 500-line ceiling. `ScoredEvent` moved with it.

### B9. PACED sources lost frames, and `--selftest` had been losing 27 of 60

**Found only because B10 made drops visible.** `07` §2 makes losslessness the property that
*defines* a PACED source — "Replay must be lossless or results are not reproducible" — and the
capture→inference edge honoured that with a blocking queue from P0. The inference→analysis edge
was drop-oldest for **every** camera regardless of timing, so inference raced ahead of analysis
and the queue evicted. `vigil run --selftest` reported `dropped=27` of 60 frames the moment the
counter was wired, and the selftest's own "no frames skipped or dropped" assertion began failing
— correctly.

`RoundRobinDropQueue.put` gains `block=True`, used for PACED cameras: it waits for room instead of
evicting, which stalls the puller, which is the correct backpressure for a source that is being
pulled. LIVE cameras are unchanged, because for them the recent picture really is worth more than
the complete one. The wait needs no timeout because `close()` releases it, and
`pipeline/shutdown.py` step 2 now closes the analysis queue alongside the frame channels — which
is what makes waiting safe rather than a way to hang the process.

### B10. `frames_dropped` was structurally always zero

`RoundRobinDropQueue.put` returned a `DropReport` and the application counted it into one
queue-specific metric. `CameraHealthTracker.record_dropped()` was called from **no production code
at all**, and `vigil_frames_dropped_total`, which `sampler.py` persists as `frames_dropped_total`,
was **never incremented anywhere**. So the camera health snapshot, the Cameras view and every
persisted `SystemMetric` reported zero dropped frames under sustained overload. The queue's own
docstring promised that "a silent drop is indistinguishable from a bug", and `05` §4's distinction
between *skipped* (design) and *dropped* (failure) was unobservable where it mattered.

Wired through `camera_runtime.record_analysis_drop()`, which writes all three. B9 is what this
immediately revealed, and is the argument for the whole exercise: an instrument that is never read
is not an instrument.

### What was deliberately **not** built

Recorded as decisions rather than omissions, and repeated in `LIMITATIONS.md`:

- **Pre-roll gap annotation in the manifest** (`07` §5 row 4). Pre-roll is retained across a
  reconnect and nothing marks that the footage has a hole.
- **Open incidents annotated `stream_interrupted`** (`07` §5 row 5). The string exists nowhere in
  the tree. An epoch reset drops pending verification and resets the ledger for that camera; an
  already-persisted ACTIVE incident is simply no longer extended.
- **Force-closing a blocked read.** `07` §5 says the watchdog should force-close a stalled source;
  [0011](0011-p0-implementation-deviations.md) records that releasing an OpenCV capture from
  another thread is not safe. A stall is detected, not cancelled — and B3 now at least makes the
  stalled worker visible.
- **The degradation ladder and automatic worker restart.** Neither is built.

---

## C. Security and dependency audit

Each constraint was checked against the code rather than asserted. Two gaps were found and closed;
two findings are recorded for the owner.

### C1. Verified as claimed

- **Parameterised queries.** Every interpolation in `persistence/queries.py` and
  `analytics/queries.py` is a literal written in that module, a run of `?` placeholders counted
  from `len(values)`, or a sort column from an allowlist that raises on an unknown key. No caller
  value reaches statement text.
- **No path traversal.** `response/paths.py::safe_join` enforces both halves — a strict
  per-component pattern **and** containment after symlink resolution — and analytics exports go
  through the same function.
- **The API deployment model.** Loopback by default; the config validator refuses a non-loopback
  bind without `api.auth.enabled` and a token of at least 32 characters; the bearer comparison is
  `hmac.compare_digest`.
- **No credentials or machine paths in committed data.** Verified across source, config, docs and
  the committed benchmark results.

### C2. Gap closed: the S608 exemption was unpaid on `analytics/`

`pyproject.toml` exempts both `persistence/**` and `analytics/**` from ruff's SQL-in-f-string rule
on a stated justification, and `tests/unit/persistence/test_query_safety.py` paid for it on one
layer only. `analytics/` inherited the exemption without a test.

`tests/unit/analytics/test_query_safety.py` now pays for it twice over: hostile payloads through
the real builders against the real schema, **and an AST check that every fragment interpolated at
every `_grouped` / `_durations` call site is a string literal**. The second is the one that
matters, because it is what survives someone threading an API query parameter through next year —
the only way this exemption could actually become unsafe, and the case ruff has been told not to
look at.

### C3. Gap closed: "no full image frames in logs" held only by caller discipline

`core/redaction.py` passed bytes and array-likes through untouched. Nothing stopped a JPEG blob or
a decoded frame being rendered into a log line; it had simply never happened. That is a statement
about today's callers, and the caller that matters is the one added next year — the same reasoning
`response/paths.py` already applies to traversal.

`summarise_opaque()` now replaces any bytes-like value over `MAX_INLINE_BYTES` (256) and anything
array-like with a short description. Array detection is **duck-typed** on `shape` and
`__array_interface__` rather than importing numpy, because `core/` is forbidden from depending on
it by the `domain-purity` contract. A sensitive *key* still wins over the summary, so a 4 KB token
is `***` and not `<bytes: 4096 bytes>`.

`scripts/gates.py` was also extended: the paths and secrets gates now cover `docs/` and `.md`
files, because `PERFORMANCE.md` and `docs/bench-results/*.json` are generated from machine data
and committed, which makes them the likeliest route for a developer's home directory to reach the
repository. The one true positive this found is the inline-credential RTSP form in `01` §10 —
quoted deliberately as the form the validator *rejects* — which carries an invisible HTML-comment
`gate: allow` marker rather than being excluded wholesale.

### C4. Finding: the `onnx-gpu` extra pulls proprietary redistributables

Read from installed metadata rather than memory: there is **no AGPL or GPL dependency anywhere**,
and `reportlab` 5.0.1 — the extra P5 added — is **BSD**, so the licensing strategy of
[0012](0012-p1-vision-decisions.md) holds.

But `onnxruntime-gpu[cuda,cudnn]` pulls `nvidia-cuda-nvrtc-cu12`, `nvidia-cuda-runtime-cu12`,
`nvidia-cufft-cu12`, `nvidia-curand-cu12` and `nvidia-nvjitlink-cu12`, all declared
**"Other/Proprietary License"** — the NVIDIA CUDA EULA. Redistribution of the runtime libraries is
permitted under that EULA's redistribution clause, so this is not a blocker, but it is **not
permissive open source** and the licensing story should say so rather than implying the whole
stack is Apache/BSD. The CPU extra has no such dependency.

### C5. Finding, deliberately not fixed: the project declares no licence of its own

There is **no `license` field in `pyproject.toml` and no `LICENSE` file**. For a project whose
entire detector-backend decision was driven by licensing, that is a real gap: without a declared
licence, the default is that no rights are granted to anyone.

It is left open on purpose. Choosing a licence is the owner's decision and not one an
implementation phase should make silently — which is the same standard this ADR applied to the
fire model.

---

## D. Build and packaging: a bare install could not start

`uv build` produces an sdist and a wheel; the wheel carries the `vigil` console script and all
five module READMEs, which the contract suite reads at runtime.

**A bare install — core dependencies, no extras — could not run at all.** `cli/main.py` imports
every command module, `commands/bench.py` reaches `bench/runner.py` and thence `pipeline/app.py`,
and eight modules down that chain `pipeline/annotate.py` did `import cv2` at module scope. The
result was a raw `ModuleNotFoundError` traceback, which is the opposite of degrading honestly, and
it took `vigil doctor` down with it — the one command an operator runs *because* something is
missing.

That matters more than it looks: `03` and [0011](0011-p0-implementation-deviations.md) made
`numpy` and `opencv-python-headless` an optional `capture` extra precisely so that capturing
frames is never a condition of starting.

New `core/optional.py` provides `optional_import` and `require`, with `EXTRA_FOR` mapping each
optional module to the extra that supplies it. `pipeline/annotate.py` and
`pipeline/evidence_media.py` — the only two modules on that chain that needed them — now import
cv2 and numpy lazily and refuse at their entry points with a `CapabilityError` naming the install
command. Verified: `vigil doctor` runs in a clean virtual environment with only the core
dependencies and reports every capability as absent with an actionable reason.

---

## E. What the P6 measurements say

`docs/PERFORMANCE.md` is regenerated from five new sustained 120 s runs
(`p6-gpu-yolox-s-{1,2,4,6,8}cam`, YOLOX-S on CUDA, 640×480, 15 fps sources, 10 fps inference
target per camera). The execution provider is verified per run rather than assumed, as
[0012](0012-p1-vision-decisions.md) B requires.

- **The measured ceiling is about 52 inferences per second.** 1, 2 and 4 cameras are served in
  full (10.0, 20.0 and 40.0 fps delivered, **zero dropped**). Six cameras demand 60 and get 52.1;
  eight demand 80 and get 51.6, with skipped frames rising from 4,546 to 8,211. So the GPU
  saturates between four and six cameras at this target rate.
- **The maximum camera count fully served at 10 fps each is four.** Beyond that the system keeps
  working and sheds load by skipping, which is the designed behaviour and is counted — but the
  per-camera rate is no longer what was configured, and `07` §8's startup oversubscription warning
  is what tells an operator so.
- **One camera has the *worst* inference latency of the five** (p50 36.5 ms, against 18.6 ms at
  six cameras), because a laptop GPU idles its clocks at low duty cycle: mean SM clock is 591 MHz
  at one camera and 1,455 MHz at eight. This confirms [0012](0012-p1-vision-decisions.md) A7 on
  new data and is the reason the sparse runs are published beside the saturated ones.
- No run was thermally throttled, and peak GPU temperature across the ladder was 66 °C — well
  under the 87 °C the P1 saturation run reached, because this ladder never saturates the GPU the
  way a 60 fps single-camera run does.

**These are throughput numbers, not accuracy numbers**, and the ladder uses synthetic test
patterns with nothing for a detector to find, so its post-processing cost is that of an empty
scene. Nothing here says anything about whether the system detects real events correctly.
