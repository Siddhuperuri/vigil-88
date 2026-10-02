# 01 — Architecture

> Status: DESIGN. No code exists.

## 1. Purpose

VIGIL-88 ingests video from one or more sources, understands what is happening in each
scene over time, and emits **verified incidents** with confidence, severity, evidence, and
an auditable explanation of why they fired.

The hard problem is not detection. Off-the-shelf detectors find people and cars. The hard
problem is **turning a noisy per-frame signal into a small number of trustworthy
incidents** — which means temporal accumulation, false-positive suppression, and an honest
confidence estimate. The architecture is organised around that problem.

## 2. Non-goals

Explicitly out of scope. Each requires a new decision record to reverse.

| Non-goal | Reason |
|----------|--------|
| Facial recognition, re-identification, identity profiling, demographic inference | Privacy posture. Not requested. Would change the system's legal character. |
| Cloud inference or telemetry egress by default | Local-first (D-010). |
| Horizontal scaling across machines | Single-node design. Nothing may *forbid* it later, but nothing is built for it now. |
| Multi-tenant access control, user accounts, RBAC | Single-operator console. Optional shared-secret auth only. |
| Training pipelines, dataset management, annotation tooling | Separate concern. Models are consumed as artifacts. |
| Mobile clients | Console is desktop-browser-first. |
| Cross-camera object identity (same person across cameras) | Deferred past P6; needs calibration work. Interface left open in `scene/`. |

## 3. System context

```
      +---------------+   +---------------+   +---------------+   +---------------+
      |   Webcam      |   |  RTSP / IP    |   | Video file    |   | Still image   |
      |  (USB/MSMF)   |   |   camera      |   | (.mp4/.mkv)   |   |  (.jpg/.png)  |
      +-------+-------+   +-------+-------+   +-------+-------+   +-------+-------+
              |                   |                   |                   |
              +-------------------+---------+---------+-------------------+
                                            |  FrameSource protocol
                                  +---------v----------+
                                  |   VIGIL-88 Core    |   single Python process,
                                  |     (headless)     |   multi-threaded
                                  +---------+----------+
                     +----------------------+----------------------+
                     |                      |                      |
            +--------v--------+   +---------v--------+   +---------v--------+
            |  SQLite (WAL)   |   | Evidence store   |   |   Model store    |
            | incidents,      |   | (filesystem:     |   | (filesystem:     |
            | events, metrics |   |  jpg/mp4/json)   |   |  .pt/.onnx)      |
            +-----------------+   +------------------+   +------------------+
                     |                      |
                     +----------+-----------+
                     +----------v-----------+          +----------------------+
                     |  HTTP + WebSocket    |<-------->|  VIGIL-88 Console    |
                     |  API (FastAPI)       |          |  (browser, React/TS) |
                     +----------+-----------+          +----------------------+
                                |
                     +----------v-----------+
                     |  Alert channels      |  console . file . webhook
                     |  (outbound, opt-in)  |  (email/SMS/MQTT = interface only)
                     +----------------------+
```

The core is usable with **no UI at all** (`vigil run --headless`). The console is a client
of a documented API, never a privileged peer. This is not a stylistic preference: it is how
the requirement "the UI must never perform expensive inference" is made structurally
impossible to violate (D-002).

## 4. Runtime topology

One OS process for the core; one for the UI dev server during development only (in
production the console is static files served by the API process).

Inside the core process, threads — **not** processes:

| Worker | Count | Nature | Why this shape |
|--------|-------|--------|----------------|
| `CaptureWorker` | 1 per camera | Blocking I/O + decode | OpenCV/FFmpeg release the GIL during decode and socket reads. Processes would mean re-decoding frames across an IPC boundary. |
| `InferenceWorker` | 1 per compute device | GPU-bound, batches across cameras | A single model instance with batched input is the only way to fit N streams in 6 GB. Multiple workers would mean multiple model copies. |
| `AnalysisWorker` | 1, serialised per camera | CPU-bound, stateful | Tracker and temporal state is per-camera and mutable. Exactly one thread may touch a camera's state (D-005). |
| `ResponseWorker` | 1 | I/O-bound | Evidence encoding, DB writes, alert dispatch. Must never block analysis. |
| `MetricsSampler` | 1 | Periodic | `psutil` + `pynvml` sampling on a fixed cadence. |
| `ApiServer` | 1 | asyncio event loop | Uvicorn. Reads immutable snapshots; never mutates pipeline state. |
| `Supervisor` | 1 | Watchdog | Liveness, restart with circuit breaker, degradation decisions. |

Windows specifics that shape this: there is no `fork`, so `multiprocessing` costs a full
interpreter spawn plus a model reload per worker — seconds of startup and a second copy of
VRAM. Threads are the correct answer here, and the GIL is not the bottleneck because the
expensive work happens inside CUDA kernels and FFmpeg, both of which release it.

## 5. Layering

Dependencies point **downward only**. A module may import from its own layer and any layer
below it. Never upward, and never sideways across sibling engines.

```
  L6  cli/              api/            <- entry points, process wiring
      -------------------------------------------------------------------
  L5  pipeline/         analytics/      <- orchestration, scheduling, graph assembly
      -------------------------------------------------------------------
  L4  ingest/  vision/  tracking/  scene/  temporal/
      events/  verification/  severity/  response/
                                        <- the engines (siblings; MUST NOT import each other)
      -------------------------------------------------------------------
  L3  persistence/                      <- repositories, migrations
      -------------------------------------------------------------------
  L2  domain/                           <- frozen dataclasses, enums. No I/O. No third-party deps.
      -------------------------------------------------------------------
  L1  config/           observability/  <- settings, logging, metrics, health
      -------------------------------------------------------------------
  L0  core/                             <- protocols, clock, ids, errors, bus, units
```

**How engines compose without importing each other:** `pipeline/` constructs them and wires
their inputs to outputs. An engine declares what it needs as a protocol in
`core/protocols/` and receives it by constructor injection. `events/` never imports
`tracking/`; it receives a `SceneState` that happens to contain tracks.

Enforcement: `import-linter` contracts declared in `pyproject.toml`, run by
`scripts/check.ps1`. A violation fails the build. This is the single most important
structural guard in the project, because modular architecture degrades silently without a
machine checking it.

## 6. The engines

Nine engines, each with one responsibility, one protocol, and a defined failure mode. Full
signatures in [04-ai-pipeline.md](04-ai-pipeline.md).

| Engine | Consumes | Produces | Holds state? | On failure |
|--------|----------|----------|--------------|------------|
| **Ingest** | source URI | `Frame` | Yes — stream epoch, health | Camera goes `RECONNECTING`; pipeline continues for other cameras |
| **Vision** | `Frame` batch | `Detection[]` | Model weights (immutable) | Camera goes `DEGRADED`; detector marked unhealthy; no incidents claimed |
| **Tracking** | `Detection[]` | `TrackedObject[]` | Yes — track table per camera | Reset that camera's tracker, bump `tracker_epoch`, log loudly |
| **Scene** | `TrackedObject[]`, zones | `SceneState` | Zone config (immutable), occupancy history | Emit `SceneState` with `degraded_facts` populated; modules requiring missing facts are skipped |
| **Temporal** | `Observation[]` | `AccumulatorState[]` | Yes — windows per subject | Drop the window, emit no candidate (fail closed) |
| **Event** | `SceneState`, via modules | `Observation[]`, `CandidateEvent[]` | Per-module state, owned and isolated | Module quarantined after N consecutive errors; other modules unaffected |
| **Verification** | `CandidateEvent` | `VerifiedEvent`, or rejection with reason | Cooldown and dedup ledger | Fail **closed** — unverifiable means rejected, never promoted |
| **Severity** | `VerifiedEvent`, context | `SeverityAssessment` | Policy (immutable) | Fall back to the event type's base severity, set `severity_degraded` |
| **Response** | `VerifiedEvent` | `Incident`, `Evidence`, `Alert` | Incident registry | Retry with backoff; persist the incident even if evidence or alerting fails, and mark it partial |

**Fail-closed vs fail-open is deliberate per engine.** Temporal and Verification fail
*closed* (no incident) because a false alarm spends operator trust, the scarcest resource
in any monitoring system. Response fails *open* (persist what it can, flag the gap)
because losing the record of a real incident is worse than an incomplete record. Each
engine states its direction in its module docstring.

## 7. Cross-cutting concerns

### 7.1 Configuration (`config/`)

Pydantic Settings v2. Layered, last wins:

```
code defaults -> config/vigil.yaml -> config/vigil.local.yaml -> VIGIL_* env -> CLI flags
```

Validated once at startup. An invalid config is a startup failure with a precise message,
never a silent fallback to a default. Schema detail in [05-data-model.md](05-data-model.md) §7.

### 7.2 Logging (`observability/logging.py`)

`structlog`. JSON lines to a rotating file; human-readable console in dev. Context is
**bound**, not formatted into message strings: `camera_id`, `frame_index`, `stream_epoch`,
`incident_id`, `correlation_id`, `stage`. A redaction processor strips anything matching
credential patterns before a record is emitted — belt and braces alongside the config rule
that credentials never become literals in config objects.

Logging MUST NOT be the explanation mechanism for a decision. Explanations are structured
data attached to the incident (§7.5), because log files are not queryable from the console.

### 7.3 Metrics (`observability/metrics.py`)

In-process registry: counters, gauges, and bounded ring-buffer time series (default 300 s
at 1 Hz, `observability.series_window_s`). Stage latency histograms with p50/p95/p99.
Exposed over the API, and optionally as Prometheus text. On the hot path a metric costs one
monotonic clock read and one array store; aggregation happens on read.

### 7.4 Errors (`core/errors.py`)

One exception hierarchy rooted at `VigilError`: `ConfigError`, `SourceError`,
`InferenceError`, `ModuleError`, `PersistenceError`, `CapabilityError`. Rules:

- No bare `except:`, and no `except Exception: pass`. Ever.
- Catch narrowly, at a boundary that can actually make a decision.
- A caught-and-swallowed error MUST increment a named metric and log at WARNING or above.
  Silent recovery is indistinguishable from a bug.

### 7.5 Explainability

Every incident carries an `IncidentExplanation`: which module fired, the observations that
contributed with their timestamps and signal values, the accumulator trajectory, every
verification validator that ran with its verdict, and the severity factor breakdown with
weights. This is a first-class domain object — persisted with the incident, surfaced in the
console, included in exports.

Rationale: a monitoring system whose decisions an operator cannot interrogate gets switched
off within a week. Explainability is a reliability feature, not a nicety.

### 7.6 Clock and determinism (`core/clock.py`)

```python
class Clock(Protocol):
    def monotonic_ns(self) -> int: ...
    def wall_utc(self) -> datetime: ...

# SystemClock  - production
# ManualClock  - tests; advanced explicitly, never sleeps
# ReplayClock  - driven by media PTS, optionally faster than real time
```

Decision logic reads timestamps from the `Frame`, which got them from an injected `Clock`.
`time.time()` and `datetime.now()` are banned outside `core/clock.py` (grep gate in CI).
Seeded RNG via `core/rng.py`. This is what makes replay and live behave identically, and
what makes the temporal engine testable without sleeping.

### 7.7 Identifiers (`core/ids.py`)

| Entity | Form | Example |
|--------|------|---------|
| Camera | operator-chosen slug, `^[a-z0-9][a-z0-9_-]{1,38}$` | `gate-north` |
| Frame | `(camera_id, stream_epoch, frame_index)` | — |
| Track | `(camera_id, tracker_epoch, track_id)` | — |
| Incident | ULID internally; display ref `INC-YYYYMMDD-XXXXXX` | `INC-20261001-7F3A2B` |
| Evidence | ULID plus sha256 of content | — |

ULIDs sort lexicographically by creation time, so timeline ordering and keyset pagination
need no secondary index. Display refs are what an operator reads aloud on a radio.

## 8. Concurrency model

```
CaptureWorker(cam)--->[LatestFrameSlot]--+
CaptureWorker(cam)--->[LatestFrameSlot]--+--> InferenceScheduler ---> InferenceWorker
CaptureWorker(cam)--->[LatestFrameSlot]--+      (token bucket)              |
                                                                           v
                                                           [AnalysisQueue, bounded]
                                                                           |
                                           AnalysisWorker (per-camera serial) <-+
                                             track -> scene -> observe ->
                                             accumulate -> promote -> verify -> score
                                                                           |
                                                           [ResponseQueue, bounded]
                                                                           |
                                                                   ResponseWorker
                                                            incident . evidence . alert
                                                                           |
                                                                    EventBus --> API --> WS
```

Invariants, each enforced by a test:

1. A camera's tracker and temporal state is touched by exactly **one** thread (D-005).
2. `Frame` pixel buffers are **never mutated** after construction. A stage needing altered
   pixels produces a new buffer. This removes the whole class of "the tracker saw annotated
   pixels" bugs.
3. Domain objects crossing a thread boundary are frozen dataclasses.
4. No lock is held across an inference call or a disk write.
5. The API reads immutable snapshots published by the pipeline and never acquires a
   pipeline lock. A slow or hung browser cannot stall detection.

## 9. Backpressure and degradation

Each queue has one **declared** policy — not an incidental consequence of a `maxsize`:

| Edge | Policy | Why |
|------|--------|-----|
| capture to inference (live) | `LATEST_ONLY`, slot of 1, overwrite | A three-second-old frame is worthless. Overwrites are counted as `frames_skipped`, kept distinct from `frames_dropped`. |
| capture to inference (file replay) | `BLOCK` | Replay must be lossless or results are not reproducible. |
| inference to analysis | `DROP_OLDEST`, bounded | Analysis lagging means the system is overloaded; keep the recent picture. |
| analysis to response | `BLOCK` with timeout, then spill to a disk journal | Losing a confirmed incident is unacceptable. |
| anything to UI | `COALESCE` at `api.push_interval_ms`, default 100 | The console needs current state, not every intermediate state. |

**Degradation ladder.** The supervisor applies these in order when
`inference_latency_p95_ms` exceeds budget, or queue depth grows, for
`pipeline.degrade_sustain_ms` (default 3000). Recovery walks back up with hysteresis —
healthy for 3x the sustain window — so it cannot oscillate:

1. Reduce per-camera `target_inference_fps` toward `min_inference_fps`.
2. Drop input resolution one tier (640 -> 512 -> 416).
3. Disable secondary models (classifiers, segmentation). Modules that need them are
   suspended and **say so in the UI**, rather than quietly producing nothing.
4. Reduce the number of actively-inferred cameras by `camera.priority`. Demoted cameras
   keep streaming video to the console and are shown as `ANALYSIS_SUSPENDED`.
5. Mark the system `OVERLOADED` and surface it on the status bar.

Every transition is logged, counted, and visible. A system that silently degrades is lying
to its operator.

## 10. Security and privacy posture

| Control | Design |
|---------|--------|
| Secrets | Never in config files, code, or logs. Env vars, `.env` (gitignored), or Windows Credential Manager via `keyring`. Config references them as `${env:CAM_NORTH_PASSWORD}`, resolved at load. |
| Credential validator | Config validation **rejects** an RTSP URL carrying inline credentials (`rtsp://user:pass@host/...`), with an error naming the env-var form to use instead. Enforced in code, not in documentation. |
| Network exposure | API binds `127.0.0.1` by default. Binding any other interface requires `api.auth.enabled=true` and a token of at least 32 chars; the validator refuses otherwise. |
| Paths | No hard-coded paths. Everything derives from `paths.data_dir` (default `./var`, overridable to a platform app dir). Path traversal is rejected when serving evidence. |
| Retention | Per evidence class, configurable: `retention.clip_days`, `retention.snapshot_days`, `retention.incident_days`. A retention worker enforces it and logs what it deleted. The default is finite, not infinite. |
| Processing locality | All inference local. No outbound network except explicitly configured alert channels. |
| Biometrics | No facial recognition, no identity profiling. A `scene/redaction.py` interface exists for optional region blurring in exported evidence; config-enabled, off by default. |

## 11. Decision register

ADR-format records. Reversing one requires a new numbered entry, not an edit to an old one.

**D-001 — Python 3.11 for the core.** The CV and inference ecosystem (PyTorch, Ultralytics,
OpenCV, ONNX Runtime) is Python-native. 3.11 is installed here and is the newest version
with unambiguous wheel coverage across the whole CUDA stack. *Rejected:* a C++/Rust core
(weeks of plumbing for throughput we have not yet shown we need); 3.12+ (wheel risk for
little gain).

**D-002 — Browser console over an HTTP/WS API, not an embedded desktop GUI.** Makes "the UI
performs no inference" a process-level guarantee, gives the highest achievable visual
identity, and lets the core run headless on a box with no display. *Cost:* video must cross
a transport — addressed in [08-ui-architecture.md](08-ui-architecture.md) §4. *Rejected:*
PySide6/Qt — one process means a UI stall can starve capture threads, and the aesthetic
ceiling is lower.

**D-003 — Modules emit `Observation`s, not incidents.** A detection module answers one
question: how strongly does this scene support event X for subject Y. Temporal
accumulation, verification, and scoring are uniform and owned by the platform. *Why:* if
each module implemented its own temporal logic, false-positive behaviour would be
inconsistent and untestable, and every new module would re-introduce the same bugs. *Cost:*
a module wanting exotic temporal reasoning must extend the temporal engine rather than
improvise. Accepted deliberately.

**D-004 — One batched inference worker per device.** A single model instance; batches
assembled across cameras. *Why:* 6 GB VRAM on a mobile part — N model copies does not fit
and wastes occupancy. *Cost:* head-of-line blocking inside a batch, mitigated by a small
`vision.max_batch_size` (default 4) and the token-bucket scheduler.

**D-005 — One thread owns a camera's analysis state.** No locks around the tracker or the
temporal windows. *Why:* lock-protected mutable AI state is the most common source of
heisenbugs in this class of system. *Cost:* per-camera analysis throughput is bounded by
one core; analysis is expected to be 1–2 ms per frame, so this is not the constraint.

**D-006 — SQLite in WAL mode, plus filesystem evidence.** Single-node, embedded,
zero-admin, transactional. Blobs live on disk referenced by path and sha256, because
databases are bad at multi-megabyte blobs. *Why:* nothing to install on a Windows desktop.
All access sits behind repository interfaces, so Postgres or Timescale is a later swap, not
a rewrite.

**D-007 — Log-odds accumulation with exponential decay and dual-threshold hysteresis.** The
temporal confidence mechanism ([04-ai-pipeline.md](04-ai-pipeline.md) §6). *Why:*
principled (independent evidence combines additively in log-odds), bounded, decays without
a hard window edge, and hysteresis prevents flapping. Monotonicity and decay are
property-testable. *Rejected:* "N of the last M frames" (brittle, discards signal
strength); averaging probabilities (a long run of weak evidence can never confirm, and
averaging is not evidence combination).

**D-008 — Capability negotiation for modules.** A module declares required capabilities
(`TRACKING`, `ZONES`, `GROUND_PLANE`, `CLASSIFIER_FIRE`, ...). The registry refuses to
activate a module whose capabilities the configured pipeline cannot supply, and the console
shows it as unavailable with the reason. *Why:* this is the mechanism that makes "no fake
functionality" structural rather than aspirational.

**D-009 — `uv` for Python dependency management.** Already installed, fast, lockfile-based,
PEP 621 native, and handles the PyTorch CUDA index cleanly. *Rejected:* Poetry (not
installed, slower, awkward with custom indexes); bare pip with requirements.txt (no real
lock).

**D-010 — Local-first, loopback by default.** See §10.

## 12. Quality gates

A change is not done until all of these pass. `scripts/check.ps1` runs them in order:

1. `ruff format --check` and `ruff check` — clean.
2. `mypy --strict` on `src/vigil/` — clean. `domain/` and `core/` additionally forbid `Any`.
3. `import-linter` — layer contracts hold.
4. `pytest` — green, with coverage at or above 85% on `domain/`, `temporal/`,
   `verification/`, `severity/`, and `events/`: the logic that decides whether to alarm.
   There is deliberately no global coverage number, which would let those be diluted by
   trivially-covered glue code.
5. `vigil doctor` — config loads, paths writable, device probe succeeds, modules register.
6. `vigil run --selftest` — the app starts, processes a synthetic clip, exits 0.
7. Grep gates — no `time.time(` or `datetime.now(` outside `core/clock.py`; no bare
   `except`; no hard-coded absolute paths.

## 13. Open questions

Tracked until resolved. Each blocks a specific phase; none blocks P0.

| # | Question | Blocks | Current lean |
|---|----------|--------|--------------|
| Q1 | Detector weights: YOLOv8n vs v8s vs RT-DETR on a 6 GB mobile part | P1 exit | Benchmark all three in P1 and decide on measured p95 latency, not reputation |
| Q2 | Fire/smoke: a dedicated tile classifier, or detector classes | P6 | Tile classifier plus temporal accumulation; detector classes for fire are unreliable |
| Q3 | Pre-roll buffer: encoded JPEGs or raw frames | P4 | Encoded — raw is about 2.6 MB per 1080p frame, so 10 s across 3 cameras blows the RAM budget |
| Q4 | Ground-plane calibration UX for velocity in m/s | P5 | Four-point homography in the zone editor; px/s until then, and labelled px/s in the UI |
| Q5 | WebRTC or MJPEG for the live wall above 4 cameras | P1 exit | MJPEG first (simple, debuggable); WebRTC behind the same `VideoTransport` interface if measurement demands it |
