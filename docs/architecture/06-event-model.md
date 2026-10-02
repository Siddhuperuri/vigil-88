# 06 — Event Model

> Status: DESIGN. No modules exist yet. The first one lands in P3.

## 1. The chain

```
  SceneState                  one per analyzed frame, per camera
      |
      |  module.observe()          pure; no clock, no I/O, no state outside the module
      v
  Observation                 "this frame supports event X for subject Y at strength s"
      |
      |  temporal accumulation     log-odds + decay + hysteresis  (04 §6)
      v
  AccumulatorState            per (camera, module, subject)
      |
      |  promotion                 threshold + support + duration all satisfied
      v
  CandidateEvent              "the system believes X is happening"
      |
      |  verification              validator chain; fails CLOSED
      v
  VerifiedEvent               "and it survived every check we know how to make"
      |
      |  severity scoring          weighted factors -> 0..100 -> band
      v
  Incident                    persisted, evidenced, alerted, explainable
```

Each arrow narrows. Each arrow is the responsibility of exactly one engine. No arrow can be
skipped: there is no path from a module to an incident, which is the single most important
property of this model (D-003).

## 2. Why modules do not create incidents

The tempting design is "each module decides when its event has occurred". It fails for
reasons worth recording, because the temptation recurs every time a module is awkward to
write:

| Problem | Consequence |
|---------|-------------|
| Every module reimplements temporal logic | Ten slightly different debouncing schemes, each with its own flapping bug |
| Temporal behaviour is untestable in general | You can test the fire module's debounce; you cannot test "the system suppresses transients" |
| Confidence becomes incomparable | Fire confidence 0.8 and intrusion confidence 0.8 mean different things, so the severity engine cannot rank them and the operator cannot triage |
| Cross-cutting suppression is impossible | Nowhere to apply "ignore everything during a camera bump", because each module already decided |
| New modules re-import old bugs | The flicker bug is fixed in six modules and present in the seventh |

So: a module answers one narrow question about one frame, and the platform owns time,
doubt, and consequence. The cost — a module that wants unusual temporal reasoning must
extend `temporal/` rather than improvise locally — is accepted, and is the right place for
that pressure to land.

## 3. Subjects

Every observation names a subject, and the subject determines which accumulator receives
the evidence. Choosing the subject correctly is the main design decision when writing a
module.

| Subject | When to use | Example |
|---------|-------------|---------|
| `track:<id>` | The hypothesis is about one object | person down, abandoned object, a specific intruder |
| `zone:<id>` | The hypothesis is about a place | crowd density in the atrium, restricted-area occupancy |
| `region:<grid>` | The hypothesis is about an image area with no tracked object | fire, smoke (not reliably trackable as objects) |
| `camera:` | The hypothesis is about the whole view | scene-wide anomaly, obstruction |

Getting this wrong has a specific, recognisable failure mode: **subject too broad** merges
independent evidence (two people in a zone look like one strong intrusion, and the incident
cannot name who), while **subject too narrow** fragments evidence (fire flickering across a
grid boundary never accumulates in any one cell, and nothing ever confirms).

Rule: the subject is whatever the operator would point at and name. For `region:` subjects
the grid is coarse (`scene.region_grid`, default 4x4) and modules MUST emit to all
overlapping cells, with the arbitration step merging neighbours (§7).

## 4. Writing a module — the complete surface

A new incident category is one directory. Nothing outside it changes.

```
events/builtin/intrusion/
  __init__.py        # exports MODULE
  module.py          # the DetectionModule implementation
  params.py          # a pydantic model for this module's parameters
  README.md          # what it detects, what it cannot, known false positives
tests/unit/events/test_intrusion.py
tests/fixtures/detections/intrusion_*.yaml
config/modules/intrusion.yaml
```

```python
# events/builtin/intrusion/module.py
MODULE = ModuleDescriptor(
    module_id="intrusion",
    event_type=EventType.INTRUSION,
    version="1.0.0",
    requires=frozenset({Capability.DETECTION, Capability.TRACKING, Capability.ZONES}),
    optional=frozenset({Capability.VELOCITY}),
    default_params={
        "object_classes": ["person"],
        "zone_kinds": ["restricted"],
        "min_track_hits": 3,
        "base_signal": 0.70,
        "velocity_bonus_ratio": 0.15,
        "edge_margin_px": 8,
    },
    description="A tracked person present inside a restricted zone while that zone is active.",
)

class IntrusionModule:
    def bind(self, ctx: ModuleContext) -> None:
        self._p = IntrusionParams.model_validate(ctx.params)
        self._log = ctx.logger.bind(module_id="intrusion")
        self._epoch: int | None = None

    def observe(self, scene: SceneState) -> Sequence[Observation]:
        if self._epoch != scene.frame_meta.stream_epoch:
            self._epoch = scene.frame_meta.stream_epoch      # rule 4: reset on epoch
        out: list[Observation] = []
        for zone in scene.zones:
            if zone.kind not in self._p.zone_kinds:
                continue
            if not zone.is_active_at(scene.frame_meta.wall_utc):
                continue
            occ = scene.occupancy[zone.zone_id]
            for track_id in occ.track_ids:
                track = scene.track_by_id(track_id)
                if track.object_class not in self._p.object_classes:
                    continue
                if track.hits < self._p.min_track_hits:
                    continue
                signal = self._p.base_signal
                if Capability.VELOCITY in scene.capabilities and track.velocity_px_s:
                    signal += self._p.velocity_bonus_ratio * _inwardness(track, zone)
                out.append(Observation(
                    camera_id=scene.frame_meta.camera_id,
                    module_id="intrusion",
                    subject=SubjectKey("track", str(track_id)),
                    signal=min(signal, 1.0),
                    t_monotonic_ns=scene.frame_meta.t_monotonic_ns,
                    facts={"zone_id": zone.zone_id, "hits": track.hits,
                           "class": track.object_class, "dwell_ms": track.zone_dwell_ms[zone.zone_id]},
                ))
        return out
```

Note what is absent: no timers, no counters of consecutive frames, no "has this already
fired", no incident creation, no evidence, no logging of decisions. The module is a pure
function of the scene plus declared parameters — roughly thirty lines, and every branch is
unit-testable with a hand-built `SceneState`.

`facts` is not decoration. It becomes the explanation an operator reads, and the fixture a
regression test asserts against.

## 5. Discovery, enablement, and parameters

1. **Discovery** — `events/registry.py` finds built-in modules by scanning
   `events/builtin/` for a `MODULE` descriptor, plus any third-party modules registered
   under the `vigil88.modules` entry-point group.
2. **Enablement** — a discovered module runs only if listed in `modules.enabled`.
   Discovery is never enablement; a dropped-in module must not start making decisions.
3. **Capability gating** — per camera, the registry resolves ACTIVE / DEGRADED /
   UNAVAILABLE (04 §4) and the result is visible in the API and the console.
4. **Parameters** — `default_params` from the descriptor, overridden by
   `config/modules/<id>.yaml`, overridden per camera by `cameras/<cam>.yaml`. Validated
   against the module's own pydantic model at startup; an unknown key is an **error**, not
   a warning, because a silently-ignored typo in a threshold is indistinguishable from the
   module not working.

Per-camera parameter overrides matter in practice: the same intrusion module wants
`base_signal: 0.7` at a fence line and `0.4` at a loading dock where people legitimately
pass through.

## 6. Verification

Validators run in a configured order against each candidate. The first `REJECT` stops the
chain. Any `DEFER` holds the candidate, which is re-evaluated on the next analysis tick
until `verification.max_defer_ms` elapses, after which it is rejected as `deferral_expired`.

| # | Validator | Rejects when | Why it exists |
|---|-----------|--------------|---------------|
| 1 | `CameraHealthValidator` | camera is `RECONNECTING`, `DEGRADED`, or the frame is stale beyond `max_frame_age_ms` | Detections from a stuttering stream have corrupted temporal spacing; any judgement built on them is unsound |
| 2 | `SceneStabilityValidator` | `stability_ratio` below threshold within `instability_blackout_ms` | Lights switching on, auto-exposure hunting, a bumped camera. In practice the largest single source of correlated false positives |
| 3 | `TrackQualityValidator` | `hits / age_frames` below `min_track_hit_ratio`, or track younger than `min_track_age_ms` | Mostly-predicted tracks are hallucinated motion |
| 4 | `MotionSanityValidator` | implied velocity exceeds a per-class physical bound | A person at 200 px/s across a 640 px frame is an ID switch, not a sprinter |
| 5 | `ZoneGateValidator` | subject is inside an `EXCLUSION` zone, or the governing zone is not active on its schedule | Spatial and temporal masking of known-noisy areas |
| 6 | `CoRequirementValidator` | the event type's structural requirements are unmet (e.g. `VEHICLE_COLLISION` needs >=2 vehicle tracks converging) | Catches a module that fired on a structurally impossible configuration |
| 7 | `DuplicateValidator` | an open incident already exists for the same `(camera, event_type, subject)` | Prevents a second incident for the same ongoing situation; instead it **extends** the open one |
| 8 | `CooldownValidator` | an incident for the same dedup key resolved under `cooldown_ms` ago | Stops resolve/refire oscillation at a boundary |
| 9 | `RateLimitValidator` | the camera exceeded `max_incidents_per_window` | An alert storm is worse than a missed alert: it destroys the operator's ability to see anything |
| 10 | `OperatorSuppressionValidator` | an operator muted this camera/zone/type and the mute has not expired | Honours "I know, the contractors are working there until 5" |
| 11 | `CapabilityCaveatValidator` | never rejects | Annotates the event with caveats for DEGRADED modules so the limitation travels with the incident |

Design notes:

- **Order is cheapest-and-most-decisive first.** Health and stability are single
  comparisons that kill whole classes of candidate; co-requirement checks are more
  expensive and run later.
- **Validators cannot promote.** `PASS` means "I have no objection", never "I am confident".
  Only accumulation can raise confidence. A validator that could add confidence would be a
  second, hidden promotion path.
- **Every verdict carries a reason string** and all verdicts are persisted on the event
  (05 §4), including passes.
- **Rejections are data.** Rejected candidates are written to `candidate_events` with their
  reason and surfaced in the console's rejection feed. This is the only way to tune
  suppression without flying blind, and it is also the fastest way to discover that a
  validator is too aggressive and silently eating real events.

## 7. Arbitration

Runs after validation, before severity. Resolves relationships between events that
validators, each looking at one candidate, cannot see.

| Rule | Behaviour |
|------|-----------|
| **Merge** | `FIRE` and `SMOKE` on overlapping regions within `arbitration.merge_window_ms` -> one `FIRE` incident with smoke as a contributing signal. Two incidents for one fire is a reporting failure |
| **Subsume** | `RESTRICTED_AREA_ENTRY` inside an active `INTRUSION` for the same track -> the specific one is recorded as a transition on the broader incident |
| **Escalate** | N incidents of the same type on one camera within `escalation_window_ms` -> a `CROWD_ANOMALY` candidate, and the severity engine's `escalation` factor rises |
| **Spatial coalesce** | `region:` subjects in adjacent grid cells with overlapping active windows -> one subject spanning the union. This is what makes the coarse grid workable (§3) |

Arbitration is deliberately a small, explicit rule set in config — not a learned or
heuristic layer. Four named rules an operator can read beat a scoring function nobody can
predict.

## 8. Severity

```
severity_score = 100 * clamp( sum_i ( weight_i * factor_i ), 0, 1 )
```

Each factor is a pure function returning [0,1], independently unit-tested, and recorded
with its raw value, weight, and contribution in the explanation.

| Factor | Default weight | Definition |
|--------|---------------|------------|
| `base` | 0.30 | The event type's intrinsic seriousness, from `severity.base_by_type`. Fire is not an equal of an abandoned bag |
| `zone_criticality` | 0.20 | `zone.criticality / 4` for the most critical involved zone |
| `confidence` | 0.15 | Post-verification confidence. An uncertain incident is still worth showing, lower |
| `object_count` | 0.10 | `min(involved / severity.count_saturation, 1)` — saturating, because 50 people is not ten times worse than 5 |
| `persistence` | 0.10 | `min(duration_ms / severity.persistence_saturation_ms, 1)` — a fire burning for 2 minutes outranks one detected 3 seconds ago |
| `time_of_day` | 0.05 | From `severity.time_policy`; an intrusion at 03:00 outranks one at 14:00 |
| `escalation` | 0.05 | Related incidents in the recent window (§7) |
| `camera_trust` | 0.05 | Per-camera multiplier reflecting that camera's historical false-positive rate, set by the operator, default 1.0 |

Score maps to a `Severity` band by `severity.bands`. Both the number and the band are
persisted: the band drives the UI and alert routing, the number drives sorting within a
band.

Three properties this design holds to:

1. **Weights sum to 1.0, validated at startup.** A config whose weights sum to 1.3 silently
   saturates every incident at CRITICAL, so the validator rejects it.
2. **Severity never feeds back into confidence.** They answer different questions — "how
   sure are we" and "how much does it matter" — and coupling them would make a dangerous
   event look more certain merely because it is dangerous.
3. **Severity is explainable as arithmetic.** The console shows the factor breakdown that
   sums to the score. If an operator disagrees with a severity, they can see which factor
   to argue with, and which config key to change.

## 9. Incident lifecycle

```
                      promote + verify
     (nothing) ----------------------------> CANDIDATE
                                                 |
                              severity scored, persisted, evidence started
                                                 v
                                             CONFIRMED
                                                 |
                              evidence complete, alert dispatched
                                                 v
             operator ack ----------------->   ACTIVE  <----- re-observation extends
                                                 |                 last_observed
                              accumulator L <= l_release
                                                 v
                                             RESOLVING
                                                 |
                              release sustained for release_sustain_ms
                                                 v
                                             RESOLVED   (terminal)

  From CANDIDATE or CONFIRMED or ACTIVE:
     operator marks false positive  -> DISMISSED   (terminal, and recorded for tuning)
     arbitration subsumes it        -> SUPPRESSED  (terminal, with the parent incident id)
     no further evidence, no confirm -> EXPIRED    (terminal; candidate timeout only)
```

Rules:

- Every transition is an `IncidentTransition` row with `actor` (`system` or `operator`),
  timestamp, and reason. The lifecycle is an audit trail, not a mutable status field.
- Terminal states are terminal. A resolved incident is never reopened; recurrence creates a
  new incident linked by `related_incident_ids`. Reopening would destroy the meaning of
  `duration_ms` and make historical analysis incoherent.
- `DISMISSED` is the most valuable state in the system for tuning. Dismissals are counted
  per `(camera, event_type, module_version)` and surfaced in Analytics as a measured
  false-positive rate — the only honest measure of whether suppression is working.
- An incident in `RESOLVING` that receives new supporting evidence returns to `ACTIVE`.
  This is the one non-monotonic edge, and it is what the hysteresis gap (04 §6.3) exists to
  make rare.

## 10. Operator actions

Exposed in the API, each producing a transition. These are real actions with real effects —
there are no decorative controls ([README](README.md) rule 1).

| Action | Effect |
|--------|--------|
| Acknowledge | `CONFIRMED -> ACTIVE`; stops alert re-notification; records who and when |
| Dismiss (false positive) | Terminal `DISMISSED`; feeds the FP rate; optionally offers to add an `EXCLUSION` zone or lower that camera's trust |
| Annotate | Attaches `operator_note`; an annotated incident is exempt from automatic retention deletion (05 §8) |
| Mute | Suppresses `(camera, zone, event_type)` for a chosen duration via `OperatorSuppressionValidator`; mutes are visible on the status bar with a countdown, never silent |
| Escalate | Raises the band by one and re-dispatches alerts; recorded as an operator override with the original score preserved |
| Export | Writes a report bundle (evidence + explanation + timeline) via `analytics/export.py` |

Mute visibility is deliberate: a muted camera that looks normal is how a real incident gets
missed.

## 11. The first three modules, and why in this order

| Order | Module | Why here | What makes it hard |
|-------|--------|----------|--------------------|
| 1 (P3) | `intrusion` | Needs only detection, tracking, and zones — all of which P1 and P2 deliver. Ground truth is unambiguous: either a person was in the polygon or not. This makes it the module that *validates the platform* rather than the one that stresses the AI | Boundary flicker; people legitimately passing near a line |
| 2 (P5) | `abandoned_object` | Exercises sequence reasoning (`object appears -> owner leaves -> object persists`) and long temporal windows, which proves the temporal engine beyond simple accumulation | Detector reliability on bags; occlusion; distinguishing "set down" from "briefly held low" |
| 3 (P5) | `person_down` | Exercises geometry and pose-adjacent reasoning (aspect ratio inversion, verticality collapse, immobility) without needing a pose model yet | Sitting, crouching, lying down deliberately. High false-positive risk, which is exactly why it comes after the verification layer is proven |

`fire` and `smoke` are last (P6) despite being the most compelling demo, because they are
the only categories whose difficulty is *perceptual* rather than *logical*. They need a
dedicated classifier and real evaluation data; building them early would mean either a
fake implementation or an undisciplined one. `vehicle_collision`, `crowd_anomaly`,
`dangerous_motion`, and `vehicle_anomaly` have descriptors in `EventType` and entries in
`docs/LIMITATIONS.md`, and no code, until there is a reason to build them.

## 12. Worked example

A person climbs a fence into a restricted yard at 02:41. Inference at 10 fps.

| t | What happens |
|---|--------------|
| 02:41:00.0 | Detector finds a person near the fence. Tracker opens track 42, `hits=1`. Module skips it: `hits < min_track_hits` |
| 02:41:00.3 | `hits=3`. Track centroid is inside `zone:yard` (RESTRICTED, criticality 3, active 18:00–06:00). Module emits `signal=0.70`. Accumulator `L = 0.85`, confidence 0.70 |
| 02:41:00.4–01.2 | 8 more observations at 0.70–0.82 (velocity bonus: moving inward). `L` climbs with decay applied each step, reaching 2.4 |
| 02:41:01.2 | Promotion: `L=2.4 >= 2.2`, observations 9 >= 4, support ratio 0.90 >= 0.6, elapsed 1200 ms >= 1200. `CandidateEvent` created |
| 02:41:01.2 | Verification: camera ONLINE, stability 0.99, hit ratio 0.92, velocity 46 px/s plausible, not in an exclusion zone, no open duplicate, no cooldown, 1 incident this hour, no mute. All PASS |
| 02:41:01.2 | Severity: base(INTRUSION)=0.65x0.30, zone 3/4=0.75x0.20, confidence 0.92x0.15, count 1/5=0.2x0.10, persistence 1200/60000=0.02x0.10, time_of_day(02:41)=1.0x0.05, escalation 0, trust 1.0x0.05 -> **score 61.9 -> HIGH** |
| 02:41:01.2 | `Incident INC-20261001-7F3A2B` persisted CONFIRMED. `first_observed=02:41:00.3`, `confirmed=02:41:01.2` -> **detection latency 900 ms** |
| 02:41:01.3 | Evidence: pre-roll clip from 02:40:55.3, raw and annotated snapshots, tracks.json, explanation.json. Alert to console and webhook |
| 02:41:14 | Person leaves the zone. Observations stop. `L` decays |
| 02:41:18 | `L = 0.74 <= 0.8` -> RESOLVING |
| 02:41:20 | Release sustained 2000 ms -> RESOLVED. `duration_ms = 19700`, post-roll clip closed |

Two things to notice. **Detection latency was 900 ms** — the price of refusing to alarm on
one frame, and the number that `min_duration_ms` trades directly against false positives;
it is measured and reported per incident rather than assumed. And the whole decision is
reconstructable from the persisted explanation: nine observations with their signals and
facts, the accumulator trace, eleven validator verdicts, and eight severity factors that
sum to 61.9.
