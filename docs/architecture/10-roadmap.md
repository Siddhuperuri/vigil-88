# 10 — Development Roadmap

> Status: DESIGN. Nothing is built. P0 has not started.

## 1. How phases work

1. **A phase ends when its exit criteria pass, not when its code is written.** Exit criteria
   are mechanical: a command runs, a test passes, a number is measured.
2. **The full quality gate (01 §12) runs at every phase boundary**, and the phase is not
   closed until it is clean. Technical debt is paid at the boundary, never carried.
3. **Each phase ends with a runnable application.** Not a library, not a branch — something
   you can start and watch do its job. A phase that ends with "the next phase will make it
   work" is mis-scoped.
4. **Each phase updates `docs/LIMITATIONS.md`** with what it did *not* implement, as a
   stated interface plus a reason. That file is the honest inventory of the gap between the
   architecture and the implementation.
5. **`docs/PERFORMANCE.md` is regenerated at every boundary from P1 onward.** No performance
   claim exists outside it.

## 2. Effort

Units are **focused working sessions** of the kind this design pass took, not calendar time.

| Phase | Sessions | Confidence in the estimate |
|-------|----------|---------------------------|
| P0 Foundation | 1 | High — no unknowns; it is scaffolding against a finished design |
| P1 Vision | 1–2 | Medium — the torch/CUDA install and model selection are where surprises live |
| P2 Tracking & Scene | 1–2 | High — self-contained, well-understood algorithms, fully synthetic tests |
| P3 Temporal & First Module | 2 | Medium — the logic is specified, but threshold tuning is iterative |
| P4 Incidents & Evidence | 1–2 | High — mostly persistence and UI against a settled data model |
| P5 Modules & Analytics | 2 | Medium — two new modules, each needing its own tuning |
| P6 Perception & Hardening | 2–3+ | **Low — see below** |
| **P0–P5 total** | **8–11** | |

**P6 is the honest unknown.** Writing a fire/smoke module against the existing plugin
contract is a few hours. Making it not fire on sunsets, headlights, brake lights,
hi-vis jackets, and steam is dataset and evaluation work measured in days of iteration
against real footage — and the footage has to be collected first. Treat P6 as open-ended
and schedule it last.

The first milestone genuinely worth showing anyone is **the end of P4**: working intrusion
detection with temporal verification, persisted incidents, captured evidence, and an
explanation an operator can interrogate. Everything before that is infrastructure;
everything after is breadth.

## 3. P0 — Foundation

**Goal:** the skeleton, fully wired and verified, with zero AI.

| Build | Detail |
|-------|--------|
| Repo scaffold | `pyproject.toml` with all tool config and `import-linter` contracts; `uv.lock`; `.gitignore`; `.env.example`; pre-commit; `git init` |
| `core/` | All protocols, `Clock` (3 impls), `rng`, `ids`, `errors`, `result`, `bus`, `registry`, `geometry`, `units` |
| `config/` | Full layered loader, `${env:}` and `${paths:}` interpolation, the complete schema from 05 §7, the credential-rejecting validator |
| `observability/` | structlog setup with redaction, metrics registry, psutil probes, health aggregation |
| `domain/` | Every type in [05-data-model.md](05-data-model.md), frozen, slotted, pure |
| `persistence/` | SQLite WAL, ORM, mappers, repositories, first alembic migration |
| `ingest/` | `FrameSource` protocol; webcam, video-file, and image sources; capture worker; health state machine; buffers |
| `vision/backends/null.py` | Returns no detections. Proves the pipeline runs end to end with no model |
| `pipeline/` | Stage graph, worker base, supervisor, snapshots, `sync` mode |
| `cli/` | `vigil run`, `vigil doctor`, `vigil replay` |
| `tests/` | All of §4, §5, §9, §12 of [09-testing-strategy.md](09-testing-strategy.md) — the generator, `MockDetector`, fixtures, `SceneStateBuilder` |
| `docs/` | `LIMITATIONS.md`, `OPERATIONS.md`, ADR directory |

**Exit criteria**

- [ ] `uv sync --group dev` succeeds with no `vision` extra, and the unit suite passes
- [ ] `vigil doctor` reports Python, paths, config validity, device probe, module registry — and correctly reports torch as absent
- [ ] `vigil doctor --dump-config` emits the complete effective config with every key, default, and unit, into `docs/CONFIG.md` (05 §7)
- [ ] `vigil run --source webcam:0` captures from the real webcam and reports live FPS, dropped and skipped counts, with the null detector
- [ ] `vigil run --source file:<clip> --sync` replays deterministically; two runs produce identical logs bar ULIDs
- [ ] `vigil run --selftest` exits 0 on a synthetic clip
- [ ] Capture survives unplugging and replugging the webcam: `RECONNECTING` -> `ONLINE`, epoch bumped
- [ ] Full quality gate clean, including `import-linter` and `mypy --strict`
- [ ] `docs/LIMITATIONS.md` exists and lists every unimplemented engine

**Not in P0:** no detector, no tracker, no zones, no modules, no API, no UI. P0 is the
hardest phase to resist adding to, and the one where that resistance pays most.

## 4. P1 — Vision

**Goal:** real detection, measured honestly, with a live view.

| Build | Detail |
|-------|--------|
| `vision/device.py` | CUDA probe, VRAM budget guard, CPU fallback, one `Device` resolved at startup |
| `vision/preprocess.py`, `batching.py`, `postprocess.py` | Letterbox with invertible transform, pinned batching, NMS, un-letterbox to source space |
| `vision/backends/ultralytics.py` | YOLO backend |
| `vision/backends/onnxruntime.py` | The licence-clean path (03 §6). **Working, not stubbed** |
| `vision/labels.py` | Label normalization with unmapped-label metrics |
| `pipeline/scheduler.py` | Token bucket, cross-camera batching |
| `pipeline/degradation.py` | The ladder (01 §9) |
| `api/` | FastAPI app, system and camera routers, MJPEG streaming with encode-once fan-out, telemetry WS |
| `ui/` | Shell, status strip, rail, Live Wall, Cameras view, design tokens |
| `cli/bench.py` | `vigil bench`, writing `docs/PERFORMANCE.md` |

**Exit criteria**

- [ ] `vigil doctor` reports CUDA available, device name, VRAM, and selected device; correctly diagnoses a CPU-only wheel with the fix command
- [ ] Detections render in the Live Wall, server-annotated, visibly synchronised with the video
- [ ] **Measured and committed** in `docs/PERFORMANCE.md`: inference p50/p95, end-to-end latency, FPS, VRAM peak, for 1–4 cameras across both backends and both precisions, over 120 s runs with the first-30 s vs last-30 s thermal delta
- [ ] Q1 decided and recorded: a weights choice justified by measured latency
- [ ] Q5 decided and recorded: MJPEG sustains the target camera count, or WebRTC is scheduled
- [ ] ONNX backend produces detections equivalent to the Ultralytics backend within tolerance (contract test)
- [ ] Batch equivalence test passes (batch 1 vs batch 4 identical within fp tolerance)
- [ ] VRAM guard refuses an oversized model with `CapabilityError` rather than OOMing
- [ ] Degradation ladder demonstrably engages under synthetic overload and recovers with hysteresis
- [ ] Console runs 1 hour with bounded memory
- [ ] Full gate clean

**This is the phase where the word "real-time" becomes usable — or does not.** If the
measured numbers do not support the camera count we want, the honest outcome is a lowered
target recorded in `PERFORMANCE.md`, not a louder claim.

## 5. P2 — Tracking and Scene

**Goal:** persistent identities and spatial understanding. Still no events.

| Build | Detail |
|-------|--------|
| `tracking/` | Kalman, assignment, ByteTrack-style association, trajectory ring buffer, velocity, epoch reset |
| `scene/zones.py` | Normalized polygons, containment, crossing lines, schedules |
| `scene/relations.py`, `occupancy.py` | Proximity, containment, per-zone counts, density, enter/exit diffing |
| `scene/analyzer.py` | `SceneState` assembly, `degraded_facts`, stability signal |
| `api/routers/zones.py` | Zone read/write, applied without restart |
| `ui/views/Zones` | Polygon editor on a live still, with client-side canvas overlay |
| `ui` | Track trails and zone rendering in the Live Wall |

**Exit criteria**

- [ ] Tracker holds a single ID across 200 synthetic frames with jitter and two brief occlusions
- [ ] ID-switch rate measured on perturbed synthetic clips and recorded
- [ ] Velocity correct within 5% against synthetic ground truth
- [ ] Zone containment property tests pass; normalization round-trips within 0.5 px
- [ ] Zones edited in the UI take effect with no restart, and survive a restart
- [ ] `tracker_epoch` bump on reconnect verifiably clears all track state
- [ ] Scene stability signal fires on a synthetic brightness step
- [ ] Full gate clean

## 6. P3 — Temporal and the First Module

**Goal:** the system stops classifying frames and starts reasoning. The architecture's
central claim becomes demonstrable.

| Build | Detail |
|-------|--------|
| `temporal/` | Window, accumulator (04 §6 exactly), hysteresis, sequence matcher |
| `events/` | Registry with discovery and capability gating, `ModuleContext`, engine, promotion |
| `events/builtin/intrusion/` | The first module (06 §4) |
| `verification/` | Engine, all eleven validators (06 §6), dedup, cooldown, arbitration |
| `severity/` | Engine, eight factors, policy, weight-sum validation |
| `api`, `ui` | Candidate rejection feed on the telemetry channel and in the console |

**Exit criteria**

- [ ] Every property test in [04-ai-pipeline.md](04-ai-pipeline.md) §9 passes
- [ ] Every integration scenario in [09-testing-strategy.md](09-testing-strategy.md) §8 passes — including *exactly one* incident for one synthetic intrusion, and *zero* for a ghost-detection-only clip
- [ ] A real person walking into a real zone on the real webcam produces exactly one candidate, with a measured detection latency inside the configured bound
- [ ] Boundary loitering for 60 s produces at most one confirm and one resolve
- [ ] A module requiring an absent capability is reported UNAVAILABLE with its reason, in the API and the console
- [ ] A deliberately broken module is quarantined without affecting the pipeline
- [ ] Rejected candidates appear in the console with reasons
- [ ] Module contract suite (09 §7) passes for `intrusion`
- [ ] Thresholds tuned against at least 30 minutes of real webcam footage, and the tuned values committed with a note on what was traded
- [ ] Full gate clean

## 7. P4 — Incidents and Evidence

**Goal:** incidents become durable, evidenced, explainable, and actionable. **First
demonstrable milestone.**

| Build | Detail |
|-------|--------|
| `response/incident_manager.py` | Lifecycle state machine (06 §9), operator actions, transitions |
| `response/evidence.py` | Pre-roll muxing, snapshots, track/scene/explanation dumps, manifest, sha256 |
| `response/retention.py` | Retention worker (05 §8) |
| `response/channels/` | console, jsonl, webhook — each with honest `is_available()` |
| `ingest/buffers.py` | `PreRollBuffer`, JPEG-encoded, byte-capped (07 §6) |
| `api` | Incident routers, evidence serving with integrity checks, mutes |
| `ui/views/IncidentDesk` | Priority queue, evidence viewer, **explanation panel**, keyboard operation |
| `ui/views/Timeline` | Incidents and camera-health events on one axis |

**Exit criteria**

- [ ] An incident survives a restart with evidence intact and hashes verified
- [ ] Pre-roll clip contains footage from before the first observation
- [ ] Evidence memory stays inside `preroll_max_mb` under sustained load — measured, not assumed
- [ ] The explanation panel reconstructs a decision completely: observations, accumulator trace, all verdicts, severity factors summing to the score
- [ ] Acknowledge, dismiss, annotate, mute, escalate, export all work end to end; no control exists that does not
- [ ] Mutes are visible with a countdown in the status strip
- [ ] Retention deletes on schedule, logs what it deleted, and never orphans an incident from its evidence
- [ ] Dismissal rate is recorded per module version
- [ ] Nightly soak: 8 hours with synthetic sources, bounded RSS and handle count
- [ ] Full gate clean, `PERFORMANCE.md` regenerated

## 8. P5 — More Modules and Analytics

**Goal:** prove the plugin architecture by using it, and close the loop on reporting.

| Build | Detail |
|-------|--------|
| `events/builtin/abandoned_object/` | Exercises the sequence matcher and long windows |
| `events/builtin/person_down/` | Exercises geometry-based reasoning |
| `scene/calibration.py` | Four-point homography; `GROUND_PLANE` capability; velocity in m/s (resolves Q4) |
| `analytics/` | Aggregations, reports, CSV/JSON/PDF export |
| `ui/views/Analytics` | By type, camera, hour; dismissal rate; detection-latency distribution |

**Exit criteria**

- [ ] Each new module was added **without modifying anything outside its own directory plus its config file** — verified by inspecting the diff. If that is false, the plugin contract failed and is fixed before the phase closes
- [ ] Both modules pass the full contract suite and have a README documenting their known false positives
- [ ] An uncalibrated camera reports velocity in px/s and labels it as such; a calibrated one reports m/s within 10% of a tape-measured ground truth
- [ ] Incident report export opens correctly outside the system and contains evidence, explanation, and timeline
- [ ] Analytics numbers reconcile against direct SQL queries
- [ ] Full gate clean

## 9. P6 — Perception and Hardening

**Goal:** the perceptually hard categories, and multi-camera scale. Open-ended.

| Build | Detail |
|-------|--------|
| Fire/smoke classifier | Tile classifier plus temporal accumulation (resolves Q2). Needs a dataset first |
| `events/builtin/fire/`, `smoke/` | With arbitration merge (06 §7) |
| Secondary-model budgeting | Two models inside the VRAM budget, with the guard enforcing it |
| Multi-camera scale | Measured ceiling for this hardware; NVDEC evaluated |
| Hardening | Long-run soak, failure injection, recovery drills from `docs/OPERATIONS.md` |

**Exit criteria**

- [ ] Fire/smoke evaluated on a held-out set with measured precision and recall, recorded — **or** the module is not shipped and `LIMITATIONS.md` says why. There is no third option
- [ ] Measured maximum camera count at an acceptable detection latency, in `PERFORMANCE.md`
- [ ] 24-hour soak with real cameras: no leak, no unbounded growth, every reconnect recovered
- [ ] Failure drills executed and documented: unplug, disk full, model deleted, config corrupted, GPU driver reset
- [ ] Full gate clean

## 10. Deliberately deferred

Named so they are decisions, not oversights.

| Deferred | Until | Why |
|----------|-------|-----|
| Remaining event types (`vehicle_collision`, `crowd_anomaly`, `dangerous_motion`, `vehicle_anomaly`) | On demand | The architecture supports them; each is a module. Building ten shallow modules is worse than three trustworthy ones |
| Pose estimation | A module needs it | A second model costs VRAM; `person_down` is attempted on geometry first |
| Cross-camera identity | Past P6 | Needs multi-camera calibration; large scope, narrow payoff here |
| WebRTC | If P1 measurement demands it | MJPEG is simpler and debuggable; the interface allows the swap |
| Postgres/Timescale | If SQLite is outgrown | Repository interfaces make it a swap |
| Email/SMS/MQTT alerts | On demand | Channel protocol exists; unimplemented channels are absent from the UI, not stubbed |
| Multi-user auth, RBAC | Not planned | 01 §2 |
| Docker packaging | Not planned | GPU passthrough friction on Windows for no gain |
| INT8 quantisation | Post-P6 | Needs calibration data and a measured accuracy baseline to compare against |
| NVDEC hardware decode | P6 evaluation | Only matters once CPU decode is the binding constraint, which measurement will show |

## 11. Working agreement

What I will do at each step, so there is no ambiguity about the process:

1. **Inspect before writing.** Read what exists; do not assume.
2. **One module per instruction.** I will not run ahead into the next phase.
3. **Run the gate and show the output.** Pass or fail, the real output. A phase reported
   complete has had its gate output shown.
4. **Report honestly.** If something does not work, I will say what, why, and what I
   propose. If a design decision in these documents turns out to be wrong once code meets
   reality, I will say so and record the change as a new decision rather than quietly
   drifting from the document.
5. **No silent scope changes.** If an exit criterion cannot be met, I will say so and
   propose either a fix or an explicit, recorded deferral — not move the criterion.
