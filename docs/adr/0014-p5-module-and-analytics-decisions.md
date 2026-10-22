# 0014 — P5: what the new modules refuse to claim, and how analytics stays honest

- **Status:** accepted
- **Date:** 2026-10-18
- **Phase:** P5 (person-down, abandoned object, analytics, reporting)

Five decisions taken while implementing P5 that depart from, or add to, the design set.
Recorded here rather than silently absorbed, per the working agreement in `10-roadmap` §11.

The thread running through all of them: a surveillance system is believed. Each decision below
is about refusing to let the implementation say more than it knows.

## A. `person_down` declares no `POSE` capability, because it uses none

`Capability.POSE` exists in the vocabulary (`05` §2) and nothing in the system grants it.
`06-event-model` §11 describes `person_down` as exercising "geometry and pose-adjacent
reasoning … without needing a pose model yet", and `10-roadmap` §10 defers pose estimation until
"a module needs it".

The implemented module requires `DETECTION` and `TRACKING`, takes `VELOCITY` as optional, and
**does not list `POSE` at all** — not as required, not as optional.

### Why

1. **An optional-but-unused capability is a lie told by the honesty mechanism.** D-008 exists so
   that a capability report tells an operator what is available and what to do about it. If
   `person_down` declared `POSE` as optional, every deployment would report the module DEGRADED,
   forever, with a reason pointing at a model that does not exist and whose absence changes
   nothing about the module's behaviour. An operator who learns that a DEGRADED badge means
   nothing stops reading the badges. That is how capability negotiation decays into theatre.
2. **Declaring it as *required* would make the module permanently UNAVAILABLE**, which delivers
   nothing to anybody.
3. **What is available supports a defensible, narrower claim.** Bounding-box geometry can show
   that something tall and moving became short, wide and still. That is worth telling an operator
   about. It cannot show *why*, so the module claims only posture, never injury.

### Consequences

- **The claim is narrowed everywhere, not just in the README.** The module descriptor, the facts
  on every observation (`method: bounding-box geometry; no pose model`), the module's README and
  `docs/LIMITATIONS.md` all say the same thing. `PERSON_DOWN` means "a person's posture appears to
  have collapsed and stayed collapsed" — not injured, not unconscious, not in medical distress.
- **The dominant false positive is unavoidable by construction.** Sitting on the ground, lying
  down deliberately, sunbathing and yoga are geometrically identical to a collapse. The module
  therefore ships *behind* the verification layer and the operator desk, not in front of them.
- **A person first seen already on the ground is harder to see than one who falls in view.** The
  collapse term is measured against the track's own trajectory maximum, so with no standing
  reference it is reported unavailable (`collapse_unavailable` in the facts) and the remaining
  weights are renormalised. A real gap, recorded as one.
- **The decision is reversible with evidence.** If a deployment has falls to detect and this
  module misses them, that measurement is what `10-roadmap` §10 was waiting for, and `POSE`
  becomes a real capability with a real provider. Nothing here blocks that.

### Rejected alternatives

- **Declare `POSE` optional and ship the same geometry.** Rejected: the degradation report would
  be permanent, unactionable and identical in every deployment.
- **Ship a lightweight keypoint model to make the claim stronger.** Rejected for this phase: a
  second model is a second set of weights, a second licence to check, a second provenance
  question and inference cost on a 6 GB laptop GPU that is already the bottleneck (ADR 0012).
- **Call the event something vaguer, like `UNUSUAL_POSTURE`.** Rejected: `EventType` is a fixed
  taxonomy (`05` §3) and inventing a member to soften a claim moves the dishonesty into the
  schema. The caveat belongs in the descriptor and the facts, where it travels with the data.

## B. `abandoned_object` accumulates the sequence instead of matching it

`06-event-model` §11 lists `abandoned_object` as the module that "exercises sequence reasoning
(`object appears -> owner leaves -> object persists`)", and `temporal/sequence.py` implements the
ordered-pattern matcher of `04` §6.4 for exactly that shape. **The module does not use it.** It
keeps the last time an owner was seen beside each object (`_owner_seen_ns`, keyed by track id)
and turns the elapsed separation into evidence.

### Why

1. **A matcher must be fed ordered marks, and the tracker blinks.** A missed detection for two
   frames is routine — it is why the tracker has a miss tolerance at all. A pattern matcher that
   saw `owner nearby` and then lost the object would reset, restarting the separation clock on
   every detection gap. "The last time an owner was seen beside it" survives the gap, because it
   is an absolute timestamp rather than a position in a sequence.
2. **These steps are continuous states, not discrete events.** "An owner is nearby" is true across
   a span of frames at varying confidence; `SequenceMatcher` is built for steps that happen at a
   point in time. Using it here would mean manufacturing edges out of a continuum, and the edges
   would fall exactly where the detector is noisiest.
3. **The accumulator is already the project's answer to noisy evidence.** Expressing separation as
   a signal lets P3's log-odds engine, its half-life decay and its hysteresis do the work they
   exist for, instead of placing a second, differently-behaved temporal mechanism beside them.

### Consequences

- **`temporal/sequence.py` still has no caller.** It is implemented and unit-tested; it remains
  the right tool for patterns whose steps genuinely are discrete events. `docs/LIMITATIONS.md`
  records that it is unused rather than implying the shipped module exercises it. This is a
  **deviation from `06` §11's stated purpose for this module**, which was to prove the matcher.
  What it proves instead is that the matcher is not the right mechanism for a state sequence
  observed through an unreliable detector — which is also useful, and is why this is written down.
- **Furniture is handled by silence, and the cost is symmetrical.** An object no owner has been
  observed beside is furniture as far as the module can tell, so it says nothing about it — ever,
  with no learned object list and no allow-list to maintain. The same rule means **a bag dropped
  before its track existed can never be reported**: before startup, before a reconnect, or before
  the tracker re-acquired it, there is no owner sighting, and the module cannot distinguish that
  bag from a bin. One mechanism, both outcomes, both documented.
- **Ownership is keyed by track id, so a lost-and-reacquired track restarts the clock.** This can
  delay a real event. The alternative is re-identification, which `01` §2 excludes.
- **An attended object produces evidence *against* (0.15), not silence.** That is what releases a
  live event promptly when an owner comes back, instead of waiting out the decay (`04` §6.1). An
  object with no watched-class stationary state produces **nothing** — a trickle of weak evidence
  about the bins would eventually confirm one of them.

### Rejected alternatives

- **Use `SequenceMatcher` with a generous reset tolerance.** Rejected: the tolerance would have to
  cover the longest plausible detection gap, at which point it is a timestamp with extra steps.
- **Fire on stationary alone and let verification sort it out.** Rejected: a bin is stationary. A
  module that fired on every stationary object on every camera forever is the fastest available
  way to teach an operator to ignore the system, and no downstream filtering fixes the volume.

## C. PDF export is implemented behind an optional extra, not as a core dependency

`10-roadmap` §8 lists "CSV/JSON/PDF export" and `02-directory-structure` §2 annotates
`analytics/export.py` as "CSV / JSON / PDF writers". So PDF **is** asked for by the design set.
But `03-dependency-strategy` §3.1 lists no renderer, and §1 rule 1 says every dependency is a
liability.

Both hold: `reportlab` is an **optional extra** (`uv sync --extra reports`), exactly as the
detector backends and the API are. With it installed, `format=pdf` produces a document. Without
it, the format **reports itself unavailable with the install command** through the same mechanism
the alert channels use, and `GET /api/v1/analytics/formats` tells the viewer which buttons to
enable.

### Why this rather than either extreme

- **A core dependency** would put a renderer, a font stack and an image library (`pillow`) into
  every install, including the headless detector-only one, for a printable report that many
  deployments will never ask for.
- **Omitting PDF entirely** would be a deviation from two design documents, justified only by a
  dependency argument the design set already weighed.
- **Silently answering `format=pdf` with CSV** would be the exact failure this project is built to
  avoid: a control that does not do what it says.

### Consequences

- **A refusal is a 503, not a 422.** The request was valid and the format is real; what is absent
  is a capability of this deployment. The body names the install command.
- **The button ships disabled and is enabled only by the server's answer**, so a failed probe
  leaves a control that cannot deliver visibly unavailable rather than enabled-then-error
  (`08` §8).
- **`reportlab` is also in the dev group**, so the full gate always exercises the PDF path. An
  extra that is only tested when someone remembers to install it is not tested.
- **Evidence is referenced, never embedded.** A frame inside a PDF is a copy of evidence outside
  the store, with no hash check on the way out and nothing to verify it against later. The
  document carries ids, relative paths and capture status; the API stays the only way to fetch a
  file (`05` §6).
- Three export formats now have to agree about the same window. They share the `Report` object
  and its `caveats`, so they cannot diverge without a test noticing.

## D. Ground-plane calibration: four points, pure Python, and no accuracy tolerance

`10-roadmap` §8 lists `scene/calibration.py` ("Four-point homography; `GROUND_PLANE` capability;
velocity in m/s"), resolving open question Q4. Built, with three decisions inside it.

1. **Four correspondences, not camera intrinsics.** Nobody calibrating a corridor camera has a
   checkerboard; anyone can measure the corners of a floor tile. Four point pairs determine a
   plane-to-plane homography exactly, which is all a floor needs.
2. **No numpy.** `03` §1 keeps the array backend in an optional extra, and `scene/stability.py`
   already degrades without it. An 8×8 solve by Gaussian elimination with partial pivoting is
   both exact enough and easier to review than a dependency, so calibration works in the bare
   install.
3. **A velocity is converted through the homography's Jacobian at the track's foot point.**
   Metres-per-pixel varies across a perspective view — by a factor of 2.5 across the example
   calibration's own quad — so a single scale factor would be wrong everywhere except where it
   was measured. The Jacobian is the derivative of the exact map, not a finite difference. The
   foot point is used because that is the part of a person on the floor; the box centre is about
   chest height, and converting there would report a speed measured on a plane that does not
   exist.

### What was removed, and why

A `max_reprojection_error_px` tolerance was implemented first and then **taken out**. An exact
four-point fit reproduces its own points to machine precision — measured at 10⁻²³ px for a quad
with a 10⁻⁷ px tilt over a 1000 px baseline — so the threshold could not fire. A check that
cannot fire is worse than no check, because it reads as protection. The reprojection error is
still measured and reported as a conditioning diagnostic; what actually guards against a
degenerate calibration is the singular-pivot test in the solve and the collinearity and
separation checks in the configuration schema, both reachable and both tested.

### Consequences

- **The accuracy of a calibration is the accuracy of the operator's tape measure, and nothing in
  the system can check it.** `10-roadmap` §8's exit criterion — "a calibrated camera reports m/s
  within 10% of a tape-measured ground truth" — **has not been met, because it cannot be met in
  this environment**: it needs a physical camera, a tape and a person walking a measured
  distance. The code path is proven against synthetic geometry with known answers. This is
  recorded in `docs/LIMITATIONS.md` as an unverified claim, not a passed test.
- **Nothing off the plane is correct**, and the homography cannot tell that its input was not on
  the floor: a staircase, a ramp, a thrown object all get a confident wrong answer. Only the foot
  point of a track is ever converted, which is the best available approximation of "on the floor".
- **A calibration belongs to one frame size.** It is refused for a different one rather than
  scaled by an unchecked factor, and `SceneFact.GROUND_PLANE` then appears in `degraded_facts`.
- **`velocity_m_s` is never derived from `velocity_px_s`.** On an uncalibrated camera it stays
  `None` and `speed_px_s` is labelled px/s, as it already was. A pixel number relabelled as
  metres would be invisible everywhere downstream.
- **A broken calibration costs the capability, not the camera.** Like a bad zone file it leaves
  `GROUND_PLANE` ungranted with a named reason and the pipeline keeps running in pixels.
- `SceneFact.GROUND_PLANE` is new; `05` §4's fact list does not include it.

## E. Additions to the design set

| Addition | Why |
|----------|-----|
| `MetricDefinition` travels with every result, and `analytics/definitions.py` holds the table | `06` §1 distinguishes raw observations, candidates, verified events, incidents and operator actions. A figure that silently mixed two of those populations would sound meaningful and be wrong, and no care at the call site prevents it. Making the population a property of the metric means a screen or an export can always say what it is counting. `Count` and `DurationStat` live in the same module because a result is only interpretable beside the definition it answers. |
| `None` rather than `0` for an uncomputable metric, all the way to the wire | On a chart a zero and an absence are indistinguishable and only one of them is true. `DurationStat.available` is false with every statistic `null`; `camera_availability` is `None` for a camera with no recorded health history, which the viewer renders as **unknown**, never as 100% online. A blind camera that looks healthy is the worst thing the dashboard could draw. |
| `Window` is closed-open and raises on `until <= since` | Adjacent windows must neither gap nor double-count. An incident at exactly `until` is outside the window — correct, and easy to seed into by accident, so a test asserts it. |
| `bucket_keys` emits empty buckets, capped at `MAX_BUCKETS = 1000` | A trend with the quiet buckets omitted reads as a shorter, busier period. The cap bounds the response instead of letting an hourly bucket over ten years build a list nobody asked for. |
| `covers_full_window` and `earliest_record_utc` on every report | A seven-day chart drawn over two days of data must not read as five quiet days. The report states the coverage it has, and the caveats are generated from the data rather than written in advance. |
| `paths.exports_dir`, and `safe_join` reused from the evidence store | A saved export is an operator-named file write. The name is **validated and replaced** rather than sanitised, so there is no half-cleaned string to reason about, and the write is confined to one directory by the same code path the evidence store uses. |
| CSV formula neutralisation, separate from quoting | `csv` quotes correctly and that is not the issue: a correctly-quoted cell beginning `=`, `+`, `-`, `@`, tab or CR still executes when a spreadsheet opens it. Cells are prefixed with `'`. An operator note is untrusted text in a spreadsheet exactly as it is in a browser. |
| `cameras.<id>.calibration_ref`, and `config/calibration/<ref>.calib.yaml` | The same shape as `zones_ref`: a short slug, never a path, so a camera cannot reach outside the config directory however its configuration is written. |
| `api/viewer.py` split into `viewer_styles.py`, `viewer_script.py` and `viewer_analytics.py` | The 500-line gate (`02` §1). The seam is the one any reader of a web page expects — structure, appearance, behaviour — with behaviour split again between the live view and the reporting panel. The two script halves are concatenated into one `<script>` element at import time. |
| The shared contract fixture gained a stationary object and an owner track | `tests/contract/test_module_contract.py` asserts properties of *every* registered module, including that it declares only capabilities it uses — which requires a scene it can say something about. The fixture grows as modules ship; that is the fixture working, not a test weakened to pass. |

## F. Corrections to the design documents

- **`06` §11's `person_down` row says "pose-adjacent reasoning".** The implemented module is
  bounding-box geometry and claims correspondingly less; §A is the record of that narrowing, and
  the module descriptor carries it at run time.
- **`06` §11's purpose for `abandoned_object` was to exercise the sequence matcher.** It does not;
  §B says why, and the matcher remains uncalled.
- **`05` §4's `SceneFact` list has no `GROUND_PLANE`.** Added, because a calibration that does not
  apply to the current frame size is a missing fact rather than a reason to stop.
- **`10-roadmap` §8 lists PDF without a renderer in `03` §3.1.** Resolved as an optional extra
  (§C) rather than by picking one document over the other.
- **`10-roadmap` §8's m/s exit criterion cannot be met in this environment.** Stated as unmet in
  `docs/LIMITATIONS.md` rather than reinterpreted into something that passes.
