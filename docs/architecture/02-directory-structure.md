# 02 — Directory Structure

> Status: DESIGN. Nothing below exists yet. This is the target created in P0.

## 1. Principles

1. **`src/` layout.** The package is not importable from the repo root, so tests exercise
   the *installed* package. This catches missing `__init__.py` and packaging mistakes that
   a flat layout hides until someone else installs it.
2. **Distribution name `vigil-88`, import name `vigil`.** Hyphens are not valid in Python
   identifiers; `import vigil` reads better than `import vigil_88`.
3. **One directory per layer, one file per concept.** A file above ~400 lines is a review
   smell and must be justified or split. No `utils.py`, no `helpers.py`, no `misc.py` —
   those are places where architecture goes to die.
4. **Runtime state never lives in source directories.** Everything written at runtime goes
   under `var/` (configurable, gitignored). The source tree stays clean enough to diff.
5. **Protocols live in `core/protocols/`, implementations live with their engine.** Callers
   import the protocol, never a concrete class.

## 2. Tree

```
Vigil-88/
|
|-- pyproject.toml              # PEP 621 metadata, deps, tool config, import-linter contracts
|-- uv.lock                     # committed lockfile (D-009)
|-- .python-version             # 3.11
|-- .env.example                # documents every secret-bearing env var; never a real .env
|-- .gitignore
|-- .pre-commit-config.yaml
|-- README.md                   # what it is, how to run it, current honest status
|-- LICENSE
|
|-- config/
|   |-- vigil.yaml              # committed defaults; no secrets, no absolute paths
|   |-- vigil.local.yaml        # gitignored; machine-specific overrides
|   |-- logging.yaml            # handler/level config
|   |-- cameras/                # one file per camera, so adding a camera is a new file
|   |   `-- example-webcam.yaml
|   |-- zones/                  # one file per camera's zone set, normalized coordinates
|   |   `-- example-webcam.zones.yaml
|   `-- modules/                # per-module parameter overrides
|       `-- intrusion.yaml
|
|-- docs/
|   |-- architecture/           # this set
|   |-- adr/                    # decision records split out of 01 §11 as they accrue
|   |-- LIMITATIONS.md          # P0 onward: every interface-only feature, with its reason
|   |-- PERFORMANCE.md          # written only by `vigil bench`; absent until measured
|   `-- OPERATIONS.md           # runbook: start, stop, recover, read the console
|
|-- src/vigil/
|   |-- __init__.py
|   |-- __main__.py             # `python -m vigil` -> cli.main
|   |-- version.py
|   |
|   |-- core/                   # L0: no dependencies on any other vigil package
|   |   |-- protocols/
|   |   |   |-- source.py       # FrameSource
|   |   |   |-- detector.py     # Detector, Classifier, Segmenter
|   |   |   |-- tracker.py      # Tracker
|   |   |   |-- module.py       # DetectionModule, ModuleContext
|   |   |   |-- validator.py    # EventValidator
|   |   |   |-- channel.py      # AlertChannel
|   |   |   |-- repository.py   # repository protocols
|   |   |   `-- transport.py    # VideoTransport
|   |   |-- clock.py            # Clock, SystemClock, ManualClock, ReplayClock
|   |   |-- rng.py              # seeded RNG factory
|   |   |-- ids.py              # ULID, camera-slug validation, display refs
|   |   |-- errors.py           # VigilError hierarchy
|   |   |-- result.py           # Ok/Err for expected failures that are not exceptional
|   |   |-- bus.py              # in-process typed pub/sub
|   |   |-- registry.py         # generic plugin registry with capability checks
|   |   |-- geometry.py         # Point, BBox, Polygon; pure math, no cv2
|   |   `-- units.py            # NewTypes: Millis, Pixels, Ratio, Mib, Fps
|   |
|   |-- config/                 # L1
|   |   |-- settings.py         # root Settings model
|   |   |-- loader.py           # layering, ${env:...} resolution, validation entry point
|   |   |-- secrets.py          # env / .env / keyring resolution
|   |   |-- paths.py            # all derived paths; the only place paths are constructed
|   |   `-- schema/
|   |       |-- camera.py       # CameraConfig, SourceConfig
|   |       |-- vision.py       # device, model, batch, resolution tiers
|   |       |-- pipeline.py     # queues, fps budgets, degradation
|   |       |-- modules.py      # enablement + per-module params
|   |       |-- severity.py     # factor weights, bands
|   |       |-- retention.py
|   |       `-- api.py          # bind, auth, push intervals
|   |
|   |-- observability/          # L1
|   |   |-- logging.py          # structlog setup, processors, redaction
|   |   |-- metrics.py          # registry, counters, gauges, histograms, series
|   |   |-- probes.py           # psutil + pynvml sampling
|   |   `-- health.py           # HealthReport aggregation
|   |
|   |-- domain/                 # L2: frozen dataclasses + enums. No I/O, no third-party deps.
|   |   |-- enums.py            # ObjectClass, EventType, Severity, IncidentStatus, ...
|   |   |-- camera.py           # Camera, CameraHealth
|   |   |-- frame.py            # Frame, FrameMeta
|   |   |-- detection.py        # Detection, DetectionSet
|   |   |-- track.py            # TrackedObject, Trajectory, TrackPoint
|   |   |-- scene.py            # SceneState, Zone, ZoneOccupancy, SpatialRelation
|   |   |-- observation.py      # Observation, SubjectKey
|   |   |-- event.py            # CandidateEvent, VerifiedEvent, AccumulatorState
|   |   |-- incident.py         # Incident, IncidentTransition, IncidentExplanation
|   |   |-- evidence.py         # Evidence, EvidenceKind
|   |   |-- alert.py            # Alert, AlertDelivery
|   |   `-- metric.py           # SystemMetric, StageLatency
|   |
|   |-- persistence/            # L3
|   |   |-- database.py         # engine, session factory, WAL pragmas
|   |   |-- orm.py              # SQLAlchemy mapped classes (separate from domain, see §4)
|   |   |-- mappers.py          # domain <-> orm translation
|   |   |-- repositories/
|   |   |   |-- incidents.py
|   |   |   |-- events.py
|   |   |   |-- evidence.py
|   |   |   |-- cameras.py
|   |   |   `-- metrics.py
|   |   `-- migrations/         # alembic
|   |
|   |-- ingest/                 # L4
|   |   |-- sources/
|   |   |   |-- opencv_base.py  # shared VideoCapture handling
|   |   |   |-- webcam.py       # MSMF/DSHOW index or name
|   |   |   |-- rtsp.py         # FFmpeg over TCP, latency options
|   |   |   |-- videofile.py    # replay, honours PTS, loop option
|   |   |   `-- imagefile.py    # single image or directory, one-shot
|   |   |-- capture_worker.py   # thread, reconnect policy, epoch management
|   |   |-- buffers.py          # LatestFrameSlot, BoundedQueue, PreRollBuffer
|   |   |-- watchdog.py         # stall detection
|   |   `-- health.py           # CameraHealth state machine
|   |
|   |-- vision/                 # L4
|   |   |-- device.py           # CUDA probe, VRAM budget guard, CPU fallback
|   |   |-- preprocess.py       # letterbox, normalize, to-tensor, pinned memory
|   |   |-- batching.py         # batch assembly and un-batching
|   |   |-- labels.py           # model-native labels -> canonical ObjectClass
|   |   |-- postprocess.py      # NMS, scaling back to source coordinates
|   |   `-- backends/
|   |       |-- ultralytics.py
|   |       |-- onnxruntime.py
|   |       |-- mock.py         # scripted detections from YAML; the test workhorse
|   |       `-- null.py         # returns nothing; used by P0 and by `--selftest`
|   |
|   |-- tracking/               # L4
|   |   |-- bytetrack.py        # IoU + Kalman association
|   |   |-- kalman.py
|   |   |-- assignment.py       # Hungarian / greedy matching
|   |   `-- trajectory.py       # history ring buffer, velocity, dwell
|   |
|   |-- scene/                  # L4
|   |   |-- zones.py            # polygon load, point-in-poly, crossing lines
|   |   |-- calibration.py      # homography, px -> m (interface + impl when P5 lands)
|   |   |-- relations.py        # containment, proximity graph
|   |   |-- occupancy.py        # per-zone counts, density
|   |   |-- redaction.py        # optional blur regions for export
|   |   `-- analyzer.py         # builds SceneState
|   |
|   |-- temporal/               # L4
|   |   |-- window.py           # time-bounded observation window
|   |   |-- accumulator.py      # log-odds accumulation with decay (D-007)
|   |   |-- hysteresis.py       # dual-threshold activate/release
|   |   `-- sequence.py         # ordered-pattern matching (A then B within T)
|   |
|   |-- events/                 # L4
|   |   |-- registry.py         # discovery, capability gating, enablement
|   |   |-- context.py          # ModuleContext handed to each module
|   |   |-- engine.py           # runs modules, collects observations, promotes candidates
|   |   `-- builtin/
|   |       |-- intrusion/      # first real module (P3)
|   |       |-- person_down/    # P5
|   |       `-- abandoned_object/  # P5
|   |
|   |-- verification/           # L4
|   |   |-- engine.py
|   |   |-- dedup.py            # cooldown ledger keyed by (camera, type, subject)
|   |   |-- arbitration.py      # cross-module merge/suppress
|   |   `-- validators/         # one file per validator; see 06 §6
|   |
|   |-- severity/               # L4
|   |   |-- engine.py
|   |   |-- factors.py          # one function per factor, each independently testable
|   |   `-- policy.py           # weights and bands from config
|   |
|   |-- response/               # L4
|   |   |-- incident_manager.py # lifecycle state machine, operator actions
|   |   |-- evidence.py         # snapshot/clip writing, hashing, manifest
|   |   |-- retention.py        # retention worker
|   |   `-- channels/
|   |       |-- console.py
|   |       |-- jsonl.py
|   |       `-- webhook.py
|   |
|   |-- pipeline/               # L5
|   |   |-- graph.py            # builds and owns the stage graph from config
|   |   |-- scheduler.py        # token-bucket inference admission control
|   |   |-- workers.py          # worker base class, lifecycle, liveness beacon
|   |   |-- supervisor.py       # restart policy, circuit breaker
|   |   |-- degradation.py      # the ladder in 01 §9
|   |   `-- snapshot.py         # immutable state snapshots published for the API
|   |
|   |-- analytics/              # L5
|   |   |-- queries.py          # aggregations over incidents
|   |   |-- reports.py          # report composition
|   |   `-- export.py           # CSV / JSON / PDF writers
|   |
|   |-- api/                    # L6
|   |   |-- app.py              # FastAPI assembly, lifespan, static console mount
|   |   |-- deps.py
|   |   |-- security.py         # optional bearer token, loopback policy
|   |   |-- streaming.py        # MJPEG multipart; VideoTransport impl
|   |   |-- ws.py               # telemetry channel, typed message union
|   |   |-- schemas/            # pydantic request/response models (not domain objects)
|   |   `-- routers/
|   |       |-- system.py       # health, metrics, version
|   |       |-- cameras.py      # list, health, stream, enable/disable
|   |       |-- incidents.py    # list, get, acknowledge, dismiss, export
|   |       |-- events.py       # timeline feed
|   |       |-- zones.py        # read/write zone definitions
|   |       `-- modules.py      # list modules, availability, params
|   |
|   `-- cli/                    # L6
|       |-- main.py             # typer app
|       `-- commands/
|           |-- run.py          # run the pipeline (+ --headless, --selftest)
|           |-- doctor.py       # environment and config diagnosis
|           |-- bench.py        # measure and write docs/PERFORMANCE.md
|           |-- replay.py       # deterministic replay of a file or a recorded session
|           |-- zones.py        # validate/convert zone files
|           `-- export.py       # incident report export
|
|-- ui/                         # console (D-002)
|   |-- package.json
|   |-- vite.config.ts
|   |-- tsconfig.json
|   |-- index.html
|   `-- src/
|       |-- main.tsx
|       |-- app/                # shell, routing, providers
|       |-- views/
|       |   |-- LiveWall/
|       |   |-- IncidentDesk/
|       |   |-- Timeline/
|       |   |-- Cameras/
|       |   |-- Zones/
|       |   |-- Analytics/
|       |   `-- System/
|       |-- components/         # primitives only; view-specific parts live with the view
|       |-- lib/
|       |   |-- api.ts          # generated client
|       |   |-- ws.ts           # telemetry socket with reconnect
|       |   |-- types.gen.ts    # GENERATED from OpenAPI; never edited by hand
|       |   `-- format.ts
|       `-- design/
|           |-- tokens.css      # the single source of colour/space/type tokens
|           `-- primitives.css
|
|-- tests/
|   |-- conftest.py             # ManualClock, tmp data dir, settings factory
|   |-- unit/                   # mirrors src/vigil/ exactly
|   |-- contract/               # every registered module/detector/channel satisfies its protocol
|   |-- integration/            # synthetic clip -> pipeline -> asserted incidents
|   |-- perf/                   # marked `slow`; excluded from the default run
|   |-- synthetic/              # the scene generator (see 09 §4)
|   `-- fixtures/
|       |-- detections/         # YAML scripts for MockDetector
|       |-- zones/
|       `-- golden/             # expected decision traces
|
|-- scripts/
|   |-- check.ps1               # the full quality gate from 01 §12
|   |-- dev.ps1                 # core + ui dev servers together
|   |-- gen_api_types.ps1       # OpenAPI -> ui/src/lib/types.gen.ts
|   `-- fetch_models.ps1        # pulls weights into var/models, verifies checksums
|
`-- var/                        # GITIGNORED. All runtime state. Nothing here is source.
    |-- logs/
    |-- evidence/
    |-- db/
    |-- models/
    `-- bench/
```

## 3. What goes where — the test for ambiguity

When unsure which directory a new file belongs in, answer in order:

1. Is it a frozen value object with no behaviour beyond validation? -> `domain/`
2. Does it describe *what* something must do rather than *how*? -> `core/protocols/`
3. Does it touch a camera, a model, a tracker, a scene, a window, a module, a validator, a
   score, or an output channel? -> the matching engine in L4.
4. Does it decide *when* or *in what order* engines run? -> `pipeline/`
5. Does it exist only to serve HTTP or a terminal? -> `api/` or `cli/`
6. Is it a pure function of numbers and geometry? -> `core/geometry.py` or `core/units.py`

If the answer is "several of these", the file is doing several things and should be split.

## 4. Why `domain/` and `persistence/orm.py` are separate

Domain objects are frozen, have no base class, and know nothing about storage. ORM classes
are mutable, inherit from `DeclarativeBase`, and are shaped by schema concerns like foreign
keys and indexes. Fusing them — the "active record" shortcut — means persistence concerns
leak into the engines and every schema migration becomes an engine refactor. `mappers.py`
is the only place that knows both, and it is boring, explicit, and heavily tested.

The cost is real: two type definitions per entity and a mapper. Accepted, because the
engines are where the complexity belongs and they must stay free of storage coupling.

## 5. Enforcement

`pyproject.toml` declares `import-linter` contracts:

| Contract | Rule |
|----------|------|
| `layers` | The L0–L6 ordering in [01-architecture.md](01-architecture.md) §5 is a strict layered contract |
| `engine-independence` | `ingest`, `vision`, `tracking`, `scene`, `temporal`, `events`, `verification`, `severity`, `response` are mutually independent |
| `domain-purity` | `domain` may import only `core` and the stdlib — no `numpy`, `cv2`, `torch`, `pydantic`, or `sqlalchemy` |
| `core-purity` | `core` imports only the stdlib (plus `ulid`) |
| `no-ui-reach-in` | nothing under `src/vigil/` imports anything from `ui/` |

`domain-purity` is the contract most likely to be argued with, because putting a numpy
array inside a domain object is convenient. The rule is: `Frame` holds an opaque handle to
pixel data typed as a protocol (`PixelBuffer`), not a `numpy.ndarray`. This keeps the
domain layer importable in a bare interpreter, which keeps it trivially testable, and
prevents array semantics (views, mutation, dtype surprises) from becoming domain semantics.
