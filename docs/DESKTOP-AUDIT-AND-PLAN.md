# VIGIL-88 — desktop audit and plan (Part A)

**Date:** 2026-11-02 · **Branch:** `master` at `094d607` · **Status:** AUDIT AND BLUEPRINT. No source code was
changed to produce this report. Part B does not start until this is approved.

**Evidence rule.** Every claim below is one of three kinds, and says which: *read* (a file in the
tree, cited), *ran* (executed during this audit, with the result), or *reasoned* (an inference with
no measurement behind it). Where a number comes from a measurement it was taken on the one
development machine: Windows 11, 15.6 GB RAM, RTX 3050 6 GB Laptop GPU on AC power, one USB webcam,
with other applications holding about 4.9 GB of the 6 GB of VRAM at the time. It is not a benchmark
claim — `docs/PERFORMANCE.md` is still the only place one may live.

Desktop phases are named **D0–D4** in this document so they cannot be confused with the engine
phases P0–P6 already recorded in the ADRs.

---

## 1. Executive summary

**The engine is sound and the desktop does not exist.** `scripts/check.ps1` ran clean during this
audit (`ALL GATES PASSED`, 1,916 passed, 2 skipped, 17 coverage floors, 3 of 3 import contracts
kept, 224 s). A live run on the real webcam confirmed the chain end to end: capture, YOLOX on CUDA
at 36 ms p50, tracking, a `person_down` event, a persisted incident with seven hashed evidence
items and an `x-evidence-integrity: ok` header, an operator acknowledgement recorded in the audit
trail, a clean 0.5 s shutdown, a restart that reopened the camera, and the incident surviving the
restart. Nothing was fabricated anywhere I looked.

**What is missing is everything a desktop product is.** There is no window, no accounts, no
login, no camera management, no zone editor, no push channel, no tray, no notifications, no
installer. The current "UI" is one 769-line embedded HTML/JS page that polls REST. Accounts, camera
management, zone editing and the first-run wizard all need new engine surface, not just new screens.

**Seven findings change the plan** (detail in §4):

1. `vigil run` **refuses to start with zero cameras** (`cli/commands/run.py`). A first-run wizard
   needs an engine that is up before any camera exists.
2. **Cameras, zones, capabilities and module bindings are computed once at start**
   (`pipeline/assembly.py::build_analysis`). Live zone apply — which P2 promised — is not a UI task:
   the `ZONES` capability is granted only if a zone file existed at startup.
3. **Every operator action is recorded as the literal string `"operator"`**
   (`response/operator_actions.py:71`). The audit table already has an `actor` column, so users are
   cheap to add, but nothing today can say *who*.
4. **There is no push channel.** No WebSocket exists in the tree; the viewer polls.
5. **Config is read-only YAML and secrets come from environment variables.** There is no write
   path, so "add a camera" has nothing to call.
6. **The server-side overlay supports `boxes`, `labels`, `info` only** (measured: `tracks,zones`
   returns 422). 08 §4's track, trail and zone overlays do not exist.
7. **A webcam is reported DEGRADED for about 8 s after every start** (ran, 2 of 2 starts:
   `low fps: 8.9 measured vs 30.0 expected`, then `recovered`). By the documented rule (06 §6
   validator 1) detection is suppressed for that window, and each start is logged as a reliability
   event.

**Recommendation.** An **Electron** shell hosting a **React/TypeScript** console, supervising the
existing **Python engine as a separate sidecar process** that serves a versioned HTTP + WebSocket
API on loopback. Login is cookie-session based and enforced in the engine. The engine is not
rewritten. This is a near-tie with Tauri on my own scoring (86% vs 87%, §5) and is decided by two
requirements Part B sets: end-to-end tests that launch the real app with accessibility checks, and
not adding Rust. **D0 begins with a measured spike that can reverse the choice.**

**Seven decisions are yours** (§13.3): the project licence, the NVIDIA runtime distribution policy
(the CUDA libraries are 2.4 GB), a code-signing certificate, update hosting and key custody, the
first-run setup-token model, whether to reverse 01 §2 (accounts and RBAC), and what a locked
console may notify.

---

## 2. Current project health

| Measure | Result | How |
|---|---|---|
| Quality gate | `ALL GATES PASSED` | **ran** `scripts/check.ps1`: ruff format and check, mypy strict, import-linter (3/3 kept), repository gates, 1,916 passed / 2 skipped in 224 s, 17 coverage floors, generated config and performance docs current, `vigil doctor`, `vigil run --selftest` (60/60 frames) |
| Source size | 226 Python files under `src/` (about 30 k lines), 101 test files, 1,452 test functions that expand to 1,916 cases | **read** |
| Largest file | `pipeline/app.py`, 497 lines, 3 lines under the 500 ceiling | **read** |
| Structure | 9 engine packages are independent siblings enforced by `import-linter`; `domain/` and `core/` are pure | **read** `pyproject.toml` |
| Dependency graph | 3,140 nodes, 9,951 edges, 137 communities (graphify, AST only). Hubs: `LoggerLike` 118, `Clock` 113, `MetricsRegistry` 97, `Application` 71 | **ran** graphify on `src/`. `Application` is the single wiring point, which matters for the supervisor and live-apply work |
| Working tree | `docs/CONFIG.md` and `docs/PERFORMANCE.md` show as modified, but `git diff --ignore-space-at-eol` is empty: **line endings only** (CRLF). `prompts/` is untracked | **ran**. A `.gitattributes` would stop the churn |
| Stale development data | `var/db` holds 100 `camera_health_events` and 12 `cameras` rows from earlier sessions; no incidents | **ran**. A shipped build needs a per-user data directory; development data must never be the default |

**Honest limit, unchanged since P6:** there is no labelled dataset, so no module has a measured
false-positive or false-negative rate and no threshold has been tuned on footage
(`docs/P4-P6-FINAL-REPORT.md` §9). Everything in this plan is about the machinery. None of it
changes what the detectors can be trusted to do.

---

## 3. Implemented versus missing

Classification key: **IV** implemented and verified · **IU** implemented but unverified ·
**PI** partially implemented · **NI** not implemented · **BL** blocked · **RI** requires improvement.
"Verified" means an automated test *and*, where marked, a live run in this audit. It never means
accurate — see §2.

### 3.1 Engine

| Feature | Class | Where | Test / evidence |
|---|---|---|---|
| Detection, YOLOX on ONNX Runtime CUDA, CPU fallback | **IV** (runs) | `vision/backends/onnxruntime.py` | `unit/vision/test_onnx_detector.py`; live: `yolox_s`, `cuda:0`, p50 36 ms, 10.0 fps. **Accuracy unevaluated** |
| Tracking (ByteTrack) | **IV** | `tracking/bytetrack.py` | `unit/tracking/test_tracker.py`, 95 % floor; live: track dump in evidence |
| Scene state, zones, occupancy, relations | **IV** | `scene/` | `unit/scene/test_scene.py` |
| Ground-plane calibration, m/s | **IV** (maths only) | `scene/calibration.py` | `test_calibration.py`, `pipeline/test_calibration_wiring.py`. The 10 % tape-measure criterion is unmet |
| Temporal accumulator, hysteresis | **IV** | `temporal/accumulator.py` | property tests `unit/temporal/test_accumulator.py`, 95 % floor |
| Eleven validators, arbitration | **IV** | `verification/` | `unit/verification/*`, 95 % floor |
| Severity, eight factors | **IV** | `severity/engine.py` | `unit/severity/test_severity.py`; live: 7 factors returned for the incident |
| Modules `intrusion`, `person_down`, `abandoned_object` | **IV** (logic) | `events/builtin/*` | `contract/test_module_contract.py`, per-module tests, `integration/test_intrusion_scenarios.py`. Live: `person_down` fired; **`intrusion` did not fire in the live run** (no candidate recorded) so it is verified on synthetic scenes only |
| `fire`, `smoke` | **BL** (declarations by design) | `events/builtin/fire/`, `smoke/` | contract suite; live: `/modules` reports `unavailable`, reason names `classification` |
| Capability negotiation | **IV** | `core/capabilities.py` | `unit/core/test_capabilities.py`; live: `/modules` reports `missing_required` and a reason |
| Incident lifecycle, dedup, transitions | **IV** | `response/incident_manager.py`, `persistence/` | `unit/response/test_lifecycle.py`, `integration/test_incident_workflow.py`; live |
| Evidence: pre/post-roll, snapshots, SHA-256 | **IV** | `response/evidence.py`, `pipeline/evidence_capture.py` | `unit/response/test_evidence.py`; live: 7 items, `x-evidence-integrity: ok`, `clip_post.mp4` 390,925 bytes |
| Retention worker | **IU** | `response/retention.py` | covered in `test_evidence.py` and `test_lifecycle.py`; never run over a real elapsed window |
| Mutes with expiry | **IV** | `response/mutes.py`, `api/routers/mutes.py` | `unit/api/test_incident_api.py`, `test_lifecycle.py` |
| Alert channels: console, JSONL, webhook | **IV** | `response/channels/` | `unit/response/test_channels_and_worker.py`; `/system/channels` reports availability honestly |
| Analytics, JSON/CSV/PDF export | **IV** | `analytics/` | `unit/analytics/*`, 95 % floor; live: `/analytics/summary` returned correct counts and `covers_full_window: false` |
| SQLite persistence, WAL, migrations | **IV** | `persistence/` | restart test live: `integrity ok`, schema version 1 |
| Webcam capture, reconnect, backoff, stall detection | **PI** | `ingest/` | `unit/ingest/test_reliability.py`; hardware test opt-in; live: opens, releases, reopens. **RI: 8 s DEGRADED warm-up on every start** (§4 F7) |
| Worker crash/stall detection | **PI** | `pipeline/health.py` | `unit/pipeline/test_workers_and_health.py`. **Detected, not restarted** |
| RTSP, video-file replay, image input | **NI** | `ingest/sources/factory.py` | camera reports FAILED: not implemented |
| Degradation ladder; worker restart | **NI** | 01 §9 | none |
| Pre-roll gap annotation; `stream_interrupted` | **NI** | 07 §5 | none |
| Spatial coalesce | **NI** | 06 §7 | needs `region:` subjects no module emits |
| `crossing_line` zone kind | **NI** | `config/schema/zones.py` | parser refuses it by design |
| `keyring` secret storage | **NI** | `config/secrets.py` | environment variables only |
| Soak and failure drills | **NI** | P4/P6 exit criteria | none run; longest sustained run is 120 s |

### 3.2 API and UI

| Feature | Class | Where | Evidence |
|---|---|---|---|
| REST: system, cameras, snapshot, detections, MJPEG | **IV** | `api/app.py` | `unit/api/test_api.py`; live |
| REST: incidents, evidence, actions, mutes, analytics, candidates, timeline, modules | **IV** | `api/routers/*` | `unit/api/test_incident_api.py`, `test_analytics_api.py`; live |
| Bearer token auth, constant-time compare | **IV** | `api/security.py` | `test_api.py`. **The `<img>` stream limitation is real and documented** |
| Overlay options | **PI** | `pipeline/annotate.py` | live: `boxes`, `labels`, `info` only. **No `tracks`, `zones` or trails** |
| WebSocket telemetry (08 §5) | **NI** | — | no WebSocket in the tree |
| Camera add/edit/remove API | **NI** | — | cameras are YAML files read at start |
| Zone read/write API, live apply | **NI** | — | `scene/analyzer.py:101 set_zones` exists and is unit-tested, but nothing calls it live |
| Config write API, settings, import/export | **NI** | `config/loader.py` is read-only | — |
| Users, roles, sessions, audit log | **NI** | — | `actor` is the literal `"operator"` |
| Logs viewer API, diagnostics bundle | **NI** | `vigil doctor` is CLI only | — |
| Seven-view console (08) | **NI** | — | provisional viewer only |
| Provisional viewer | **PI** | `api/viewer*.py`, 769 lines | live audit in §8: no landmarks beyond `header`, no `h1`, no `aria-live`, no reduced-motion rule, no light theme, fixed two-column grid |

### 3.3 Desktop, packaging, security

| Feature | Class | Note |
|---|---|---|
| Window, menu, tray, notifications, autostart, single-instance, global shortcuts | **NI** | no shell exists |
| Installer, uninstaller, update channel | **NI** | `uv build` makes a wheel; `dist/` holds `vigil_88-0.1.0` wheel and sdist only |
| Licence | **BL** | no `LICENSE`, no `license` field. A shipped installer without one grants no rights to anyone |
| NVIDIA redistributables | **PI** | `onnx-gpu` extra pulls five CUDA wheels under the CUDA EULA; permitted, not permissive |
| Code signing | **BL** | needs a certificate. Not available here |

### 3.4 Duplicated functionality and technical debt

- **Four hand-written JavaScript/CSS strings inside Python** (`viewer.py`, `viewer_script.py`,
  `viewer_analytics.py`, `viewer_styles.py`) carry per-file `E501` ignores. They are not testable
  as frontend code and will be deleted by D1, not extended.
- **`SeverityName` is mirrored** between `config/schema/alerts.py` and `domain/enums.py`, guarded by
  a test (`test_taxonomy.py`). Correct, and worth knowing before adding any new enum to the API.
- **Constants in `api/app.py`** (`_COUNTERS`, `_CAMERA_ID`) belong with the schemas once a typed
  client is generated; leave until the OpenAPI types job exists.

---

## 4. Critical architectural findings

Each finding states the cost of ignoring it.

**F1 — The engine cannot start empty.** `run.py` exits with `no enabled cameras` and status 2. The
first-run wizard (admin, GPU check, model download, first camera) needs a running engine *before*
a camera exists. **Fix in D0:** a `vigil serve` entry that starts the API, auth and persistence with
zero cameras and adds them at runtime.

**F2 — Camera set, zones and capabilities are frozen at startup.** `build_analysis` loads each
camera's zone file once, grants `ZONES` only if it defined a zone, and builds module bindings from
that. `SceneAnalyzer.set_zones` can swap polygons live, but a camera that started with no zone file
has no `ZONES` capability, so `intrusion` is bound `UNAVAILABLE` and stays that way. A zone editor
that saves a polygon and silently does nothing is exactly what 08 §8 forbids. **Fix in D2:** a
`CameraManager` that can add, remove and rebuild one camera's runtime, and re-run capability
negotiation for that camera. Until then the honest behaviour is "apply by restarting that camera's
analysis", with the UI saying so.

**F3 — No identity.** `actor="operator"` is hard-coded at three call sites in
`response/operator_actions.py` (the other transitions use `"system"`). `incident_transitions.actor` and `mutes.actor` are already `TEXT NOT
NULL`, so storing a user id needs no schema change. The cost is passing an actor through
`IncidentActions` without letting `response/` import an auth layer: the actor must arrive as a
parameter, never be looked up.

**F4 — No push channel.** 08 §5 specifies one WebSocket with coalesced pushes. None exists, so the
console cannot show a new CRITICAL promptly without polling, and a UI that polls every second burns
the one budget — the loopback — the architecture meant to keep free. **Fix in D1.**

**F5 — No configuration write path.** The loader is layered and read-only; `docs/CONFIG.md` is
generated from the schema. A desktop product needs a validated, atomic writer (temp file and
`os.replace`) using the *same* pydantic models, so a UI cannot produce a file the loader rejects.

**F6 — The overlay set is smaller than 08 promises.** The brief asks for trails, zone overlays and
per-overlay toggles. Server-side annotation currently draws boxes, labels and an info line. Adding
`tracks`, `trails` and `zones` is a change to `pipeline/annotate.py`, which sits at 182 lines, so it
fits. The encode-once fan-out still holds (measured, §5.4).

**F7 — Warm-up DEGRADED on every start (ran).** `camera_health_events` for the live webcam shows,
on both starts: `online`, then about 1 s later `degraded — low fps: 8.9 measured vs 30.0 expected`,
then `recovered` about 8 s later. ADR 0015 §B1 made the expectation adapt to the sustained rate, but
during warm-up the camera is still held DEGRADED against the driver's claim, and DEGRADED makes
verification reject every candidate. Consequences: the first ~8 s of every start cannot confirm an
event, and each start adds a false reliability dip to analytics. **Not investigated further** — D0
task, with a test that a steady 10 fps camera is never DEGRADED on a 30 fps claim.

**F8 — Streams deliver below the configured rate (ran).** Configured `api.stream_fps: 10`;
measured 8.1–8.9 fps per viewer, with inference at 10.0 fps per camera and no frames dropped. Cause
not found. A D1 investigation, because it is the number an operator will see.

**F9 — Process model.** The API runs on a daemon thread of the engine process
(`api/server.py`). A GIL-bound `/system/metrics` call and detection share a process. Measured cost
is small (§5.4), so this does not force a split, but it does mean *the engine and API cannot crash
independently*. The shell supervising the whole engine process (§5.2) is where that isolation
belongs.

**F10 — Evidence integrity is shown nowhere an operator can see.** The server verifies and sets
`x-evidence-integrity`; no UI renders it. The viewer's explanation panel is the only consumer. G
("SHA-256 integrity verification shown to the operator") is therefore half done.

**Findings that did *not* turn out to be problems:** a clean shutdown releases the camera (0.5 s,
and the reopen worked); the API holds no pipeline lock beyond the results store; viewers cost the
engine almost nothing (§5.4); the layering held under graphify (`Application` is a hub but nothing
below `pipeline/` imports it).

---

## 5. Recommended desktop architecture

### 5.1 The options, scored

Weights are my judgement and are shown so you can change them. Scores are 1 (poor) to 5 (strong).
"Measured" marks the only figures taken in this audit; everything else is reasoned and should be
re-checked by the D0 spike.

| Criterion | Weight | Electron | Tauri 2 | PySide6 (Qt) | pywebview | Basis |
|---|---:|---:|---:|---:|---:|---|
| Works with the existing Python engine and CV pipeline | 15 | 5 | 5 | 3 | 3 | Electron, Tauri and pywebview are HTTP clients of a sidecar, so the engine is untouched. Qt would discard the 08 web design and the React console |
| GPU and ONNX Runtime, CUDA DLL loading | 10 | 5 | 5 | 4 | 5 | The engine is a separate process in every sidecar option, so `vision/device.py`'s DLL-path handling is unchanged. In-process Qt would share a DLL search path with Qt's own |
| Memory footprint and startup | 10 | 2 | 4 | 4 | 4 | **Measured proxy:** Edge in app mode (the WebView2 engine) rendering 4 MJPEG tiles: 9 processes, 523 MB working set, 638 MB private. Electron carries its own Chromium, so expect more than that, **not measured**. Tauri's shell is small but pays the same renderer cost |
| UI performance with 4+ live streams | 10 | 4 | 4 | 3 | 4 | Chromium decodes MJPEG `<img>` natively and off the main thread. Qt would need its own MJPEG client in Python |
| Device and native integration (tray, toasts, shortcuts, autostart, dialogs, single instance) | 10 | 5 | 5 | 4 | 2 | pywebview has no tray or notification API |
| Windows packaging and installer complexity | 10 | 4 | 4 | 3 | 3 | electron-builder is mature (NSIS). Tauri needs a Rust and MSVC toolchain, **not installed here** (`cargo` and `rustc` absent). Qt and PyInstaller together are fiddly |
| Code signing and signed auto-update | 10 | 3 | 4 | 1 | 1 | Tauri's updater verifies a minisign signature with a key you generate, no certificate needed. electron-updater verifies the Authenticode publisher, which needs a purchased certificate, unless a custom `verifyUpdateCodeSignature` is supplied |
| Licensing | 5 | 5 | 5 | 3 | 4 | Electron MIT (v45.2.1), Tauri Apache-2.0 OR MIT (CLI 2.12.4), PySide6 LGPL-3 or GPL or commercial (6.12.0), pywebview BSD-3 |
| Future cross-platform | 5 | 5 | 4 | 5 | 3 | Electron pins one Chromium everywhere. Tauri uses WebKitGTK and WKWebView off Windows, whose rendering and accessibility differ |
| Maintainability and testability | 10 | 5 | 3 | 2 | 2 | Playwright launches Electron directly (`_electron.launch`) and runs axe-core against the real window. Tauri on WebView2 needs a remote-debugging workaround. Tauri adds Rust as a second language. Qt widgets cannot be tested with axe |
| How much existing code survives | 5 | 5 | 5 | 2 | 4 | The REST API, schemas and the 08 design survive in all three web options. The embedded viewer is deleted in all of them |
| **Weighted total (of 500)** | | **430 (86 %)** | **435 (87 %)** | 336 (67 %) | 316 (63 %) | |

**Reading the result honestly.** Electron and Tauri are a tie: a one-point margin is inside the
noise of weights I chose. PySide6 and pywebview lose clearly. Anything not in this table — a
native WinUI 3 or WPF shell hosting WebView2 — would score like Tauri on footprint and worse on
cross-platform; it was not pursued.

### 5.2 Recommendation: Electron shell, React console, Python engine sidecar

```
                 +---------------------------------------------------------------+
  Windows user   |  VIGIL-88.exe  (Electron main process, TypeScript)            |
                 |    * single-instance lock, tray, native notifications,        |
                 |      global shortcuts, file dialogs, autostart, menu          |
                 |    * ENGINE SUPERVISOR: spawn, ready-handshake, health poll,  |
                 |      restart with backoff, graceful stop, crash report        |
                 |    * holds no secrets and no business logic                   |
                 +-----------+-------------------------------+-------------------+
                             | preload IPC (narrow,          | child_process
                             | typed, no Node in renderer)   | stdio JSON lines
                 +-----------v-----------+       +-----------v-------------------+
                 | Renderer: React + TS  |       | vigil-engine.exe (Python)     |
                 | no inference, no      | HTTP  |   vigil serve                 |
                 | authoritative state   +------>|   FastAPI /api/v1 + WebSocket |
                 | one origin only       | WS    |   auth, sessions, roles, audit|
                 +-----------------------+ cookie|   pipeline, ONNX, SQLite      |
                                                 |   127.0.0.1:<ephemeral port>  |
                                                 +-------------------------------+
```

- **The renderer talks to the engine over HTTP and one WebSocket on loopback**, from the origin the
  engine serves. It never receives Node or a filesystem. A UI crash cannot take the engine down; an
  engine stall cannot freeze the UI because the renderer only waits on network calls and shows
  "engine not responding" with a restart control.
- **The shell supervises the engine process**: it starts it with a one-time setup token and an
  ephemeral port, waits for a ready line on stdout carrying the port, polls `/system/health`,
  and on exit restarts it with exponential backoff (1, 2, 4 … 30 s, reset after 60 s healthy),
  showing the operator what happened. A restart releases the camera (verified in this audit).
- **Worker-level restart and the degradation ladder stay inside the engine**, in `pipeline/`, where
  the heartbeat data already lives (`core/heartbeat.py`, `pipeline/health.py`). The process
  supervisor is the outer ring; worker restart is the inner ring. Neither replaces the other.
- **The engine is not rewritten.** What changes in it is additive: `vigil serve`, an `auth/` layer,
  a `CameraManager`, a WebSocket hub, a config writer, new routers. `domain/`, `temporal/`,
  `verification/`, `severity/`, `events/` and the incident machinery are not touched, so D-003 and
  the accumulator's mathematics are untouched by construction.
- **Not a Windows service.** A service runs in session 0, where access to a user's webcam is
  restricted, and cannot show a tray icon. Background monitoring means *the tray app running
  minimised at login*. That reasoning is not tested here; if it matters, test it in D0 before
  promising "monitors with nobody logged in".

**Why not the others, in one line each.** *Tauri:* the better update-signing story and a smaller
shell, rejected because Part B requires end-to-end tests that launch the real app plus axe checks,
because it adds Rust to a Python and TypeScript project, and because the toolchain is not installed;
it remains the fallback. *PySide6:* would be a second UI toolkit beside a web design already
specified, with weaker accessibility testing and LGPL obligations. *pywebview:* no tray, no
notifications, no updater.

**D0 spike, with an exit that can reverse this.** Before any screen is built, D0 produces a
skeleton Electron shell that spawns the engine and renders a 4-tile wall, and measures, on this
machine: cold start to first frame, idle and 4-tile private memory of the whole process tree, and
VRAM used by the Chromium GPU process (**other applications already hold about 4.9 of 6 GB**, so
this matters; `app.disableHardwareAcceleration()` is the lever). Budgets proposed: shell tree under
**700 MB private** with 4 tiles, first frame within **8 s** of launch with a warm model cache, and
under **150 MB** additional VRAM. Missing two of the three means the Tauri fallback is evaluated
before D1 starts.

### 5.3 The local boundary: API and security of the channel

- **Transport:** HTTP and WebSocket on `127.0.0.1`, ephemeral port chosen by the supervisor. Another
  local user or process *can* reach a loopback port, so the boundary is authenticated, not hidden
  (§7).
- **Origin binding:** every request must carry a `Host` of the loopback origin and, for state
  changes and WebSocket upgrades, a matching `Origin`. This blocks DNS-rebinding and a web page in
  the user's ordinary browser from driving the engine.
- **Versioned:** `/api/v1` is kept and extended compatibly. Breaking changes get `/api/v2`. The
  OpenAPI document is the contract and TypeScript types are generated from it (08 §5 rule 6); a
  backend rename becomes a compile error.
- **No engine logic in the shell.** Anything the operator can do is an API call that the engine
  authorises. The shell only adds what a web page cannot do: tray, notifications, shortcuts,
  dialogs, process control.

### 5.4 How video reaches the UI — measured

Configuration: engine on the real GPU with `yolox_s`, four synthetic 640×480 cameras at 15 fps,
inference target 10 fps each, 0 frames dropped; viewers were separate HTTP clients plus, for the
browser-side figure, Edge in app mode. **Caveat: the synthetic source is a smooth gradient that
compresses far better than a real scene.** One real-webcam snapshot at 1280×720 with boxes was
20,617 bytes, so a real tile is nearer 1.6 Mbit/s at 10 fps (a single sample, not a measurement of
the stream).

| Condition | Engine CPU (share of machine) | Engine RSS | Per-viewer rate | Per-viewer bitrate |
|---|---:|---:|---:|---:|
| No viewers | 4.3 % | 914 MB | — | — |
| 4 viewers, one per camera | 4.6 % | 919 MB | 8.5–8.9 fps | 831–875 kbit/s |
| 8 viewers, two per camera, same overlay | 5.1 % | 920 MB | 8.1–8.8 fps | 795–864 kbit/s |
| 8 viewers, two per camera, *different* overlays | 5.1 % | 920 MB | 8.4–8.7 fps | 614–854 kbit/s |

| Browser side (Edge in app mode, 4 `<img>` tiles, 20 s) | Result |
|---|---|
| Processes | 9 (1 browser, 1 GPU, 3 renderer, 3 utility, 1 crashpad) |
| Working set / private memory | 523 MB / 638 MB (working set double-counts shared pages) |
| CPU, whole tree | about 0.4 % of the machine (a short undercount: two short-lived processes exited during sampling) |

**Reading it.**

- **MJPEG over loopback is adequate for the wall today.** Adding viewers is nearly free for the
  engine, and 08 §4's encode-once fan-out is confirmed: eight viewers cost the same as four.
- **Different overlay sets still share cost** at this scale because encoding is cheap on this
  content; on real footage and 8 distinct combinations that needs re-measuring before the overlay
  toggle UI ships.
- **The measured 8.1–8.9 fps against a configured 10 is finding F8**, not a transport limit.
- **WebRTC is rejected for now:** it needs a video encoder (H.264 through ffmpeg or NVENC) and a
  signalling stack, solving a bandwidth problem loopback does not have.
- **Shared memory is rejected:** an Electron renderer cannot map it without a native addon.
- **Binary WebSocket frames with the detection set in the same message** are the upgrade path if
  server-side overlays prove too limiting. They would give a client-drawn, zoomable, toggleable
  overlay that is synchronised by construction, which the `<img>` tag cannot do (08 §4). That is a
  D3 candidate, behind the existing `VideoTransport` protocol, and only if measurement asks for it.
- **Authentication of the stream** is by session cookie (§7), which an `<img>` sends automatically.
  That removes the limitation recorded in `docs/LIMITATIONS.md`.

---

## 6. Complete feature inventory

**Exists** = in the tree and verified above. **Proposed** = designed here, not built. **Cannot** =
impossible or dishonest on this codebase and hardware, with the reason. *UI only* means the API
already exists.

### A. Dashboard

| Item | State | Note |
|---|---|---|
| Camera count and health, active incidents, recent events | Proposed (UI only) | `GET /cameras`, `/incidents`, `/events/timeline` |
| Live detection stats, FPS, latency, stage latencies | Proposed (UI only) | `/system/metrics` |
| Severity distribution | Proposed (UI only) | `/analytics/summary.by_severity` |
| CPU, RAM, GPU, VRAM | Proposed (UI only) | GPU without NVML must read "unavailable", never 0 % |
| Storage health | Proposed | small new endpoint: free space, evidence bytes |
| Application health | Exists | `/system/health` |

### B. Camera management

| Item | State | Note |
|---|---|---|
| Add, edit, remove | Proposed | needs F2 (`CameraManager`) and F5 (config writer) |
| Webcam | Exists | MSMF fails on this machine; DirectShow works |
| RTSP | Proposed | spike first: OpenCV's bundled FFmpeg versus PyAV (§13.2) |
| Video file replay | Proposed | PACED, lossless, PTS-driven (07 §2) |
| Image input | Proposed, low value | useful for tests and zone drawing |
| Connection test | Proposed | open, read one frame, report resolution and measured fps, close |
| Auto-reconnect, health | Exists | `ingest/backoff.py`, `ingest/health.py` |
| Names, priority | Exists | groups are Proposed |
| Per-camera resolution, FPS, modules | Exists in config, Proposed in UI | |
| Live preview | Exists | snapshot and MJPEG |
| Device enumeration by name | Proposed | indices are unstable on Windows; a small package returning DirectShow and MSMF names, licence verified at pin time |
| Camera credentials | Proposed | Windows Credential Manager through `keyring`; config holds a reference, never the secret |

### C. Live monitoring

| Item | State | Note |
|---|---|---|
| Wall layouts 1 / 2×2 / 3×3 / custom, expanded, full screen | Proposed (UI only) | the current viewer is a fixed two-column grid |
| Boxes, labels, confidence | Exists | `overlay=boxes,labels,info` |
| **Track IDs, trails, zone overlays** | **Proposed, not implemented** | F6 |
| Per-overlay toggles | Exists for the three above | a real server-side parameter |
| Connection and detection state per tile | Exists in data, Proposed in UI | |
| Live event indicator | Proposed | needs F4 |
| Snapshot capture | Exists | `snapshot.jpg`; Proposed: save dialog |
| Pause or resume one camera | Proposed | `enable` and `disable` are in 08 §11, not built |

### D. Detection intelligence

| Signal | State | Honest statement |
|---|---|---|
| Person, vehicle detection | Exists | COCO classes via YOLOX; **accuracy not evaluated** |
| Tracking | Exists | |
| Intrusion, restricted zones | Exists | verified on synthetic scenes; no live firing observed in this audit |
| Person down | Exists | bounding-box geometry, no medical claim; fired live once, correctness not judged |
| Abandoned object | Exists | needs an owner seen to leave |
| **Fire, smoke** | **Cannot** | declarations only; needs a licensed, evaluated model. The UI says "not available in this build" |
| Line crossing, direction of travel, counting | Proposed | geometry over tracks, no model; needs the `crossing_line` zone kind and a new module under D-003; same "unevaluated" label |
| Vehicle collision, crowd anomaly, dangerous motion | **Cannot** | no module, no data; a heuristic would be presented as perception (rule 5) |
| Per-camera thresholds | Proposed | per-module params exist; a per-camera override needs a layered store |
| Capability indicators | Exists | `/cameras/{id}/modules` |
| False-positive review loop | Proposed | dismissal records a reason today; add a structured reason list and a review queue. **A dismissal rate is not a false-positive rate** and the UI keeps saying so |

### E. Zone management

| Item | State | Note |
|---|---|---|
| Polygon drawing on a live still | Proposed | normalised coordinates, validated by the same `config/schema/zones.py` |
| Restricted, exclusion, counting, region-of-interest | Exists in schema | |
| Entry and exit lines | **Cannot yet** | `crossing_line` is refused until a consumer exists |
| Criticality, schedules with weekdays and midnight wrap | Exists in schema | no UI |
| Enable or disable | Proposed | |
| Applied without restart | Proposed, blocked on F2 | until then the UI says "applies after this camera's analysis restarts" |
| Zone history | Proposed | persist each saved version with user and time in the audit log |

### F. Incident management

| Item | State | Note |
|---|---|---|
| Queue, severity, type, status, filters, keyset paging | Exists | `GET /incidents` |
| Acknowledge, dismiss, escalate, annotate | Exists | the `permissions` block says why an action is unavailable |
| **Resolve (operator)** | Proposed | `resolved_utc` exists, no operator route; the lifecycle transition must be added carefully |
| Search | Proposed | filters exist; free text over notes and reasons does not |
| Per-incident timeline | Exists | `transitions` |
| Evidence preview and playback | Exists in API, Proposed in UI | `accept-ranges: bytes` is served, so scrubbing works |
| Explanation panel | Exists | observations, accumulator trace, validators, severity factors |
| Export | Exists | `/analytics/export`; a single-incident bundle is Proposed |

### G. Evidence

| Item | State | Note |
|---|---|---|
| Automatic capture, pre/post-roll, snapshots, manifest | Exists | |
| SHA-256 verified on read | Exists | **not shown to the operator (F10)** |
| Retention, storage use | Exists, UI Proposed | |
| Export | Partial | references only; no single-file bundle |
| **Gap annotation across reconnects** | **Not implemented** | an operator cannot tell a clip spans a hole |

### H. Alerts and notifications

| Item | State | Note |
|---|---|---|
| In-app toast, banner, screen-reader announcement | Proposed | needs F4 |
| Native OS notification | Proposed | Electron `Notification`; Windows needs an app identity set by the installer |
| Audio with severity mapping | Proposed | bundled sounds; user files need a copy-in step |
| Per-camera rules, quiet hours | Proposed | quiet hours must not suppress CRITICAL by default |
| Mutes with countdown | Exists | |
| Acknowledgement, history | Exists | |
| Webhook | Exists | the only outbound network call |
| Email | **Cannot until configured** | needs an SMTP account; an unconfigured channel is absent, not a disabled stub |

### I. Analytics and reports

| Item | State | Note |
|---|---|---|
| Trends, by camera, severity, hour | Exists | |
| Detection latency, time-to-acknowledge, time-to-resolve | Exists | `available: false` when there is no data |
| Dismissal rate per module version | Exists | no data yet |
| Camera reliability | Exists | **polluted by F7** until fixed |
| Daily, weekly, monthly; CSV, JSON, PDF | Exists | PDF behind the `reports` extra |
| Charts | Proposed | inline SVG, no chart library (03 §5) |

### J. Settings

General, detection, models (explicit fetch with source, size and SHA-256 shown), GPU or CPU,
storage, retention, notifications, logging, startup, performance profile
(`pipeline.global_inference_fps`, stream rate), config import and export with secrets never
exported, restore defaults: all **Proposed**, all dependent on F5.

### K. System monitoring

CPU, GPU, VRAM, RAM, FPS, latency, queue depth, skipped versus dropped, worker heartbeat and stall:
**Exists** in the API. Errors and a **diagnostics bundle** (versions, redacted config, logs, health,
capability table; never frames or credentials) and a size-capped **logs viewer** over
`logs/vigil.jsonl`: **Proposed**.

### L. Desktop integration

All **Proposed** and shell-owned: native window and menu; tray with minimise-to-tray; background
monitoring while the tray app runs; start at login; native notifications; global shortcuts; native
file dialogs; single-instance lock; crash recovery by supervisor restart; local database management
(integrity check, backup); installer and uninstaller; signed auto-update; about; first-launch
wizard. Offline operation already holds (no required network). Graceful shutdown that releases the
camera is **verified at engine level**.

### M. Accounts and security

Entirely **Proposed**, and it reverses an existing decision. See §7.

---

## 7. Accounts and security design

This reverses 01 §2 and 10-roadmap §10 ("multi-user auth and RBAC: not planned"). It needs a
superseding record, **ADR 0016**, written in D0. The reasons are concrete: a shared workstation, an
operator action that must name a person, and a live stream that cannot authenticate today.

### 7.1 Principles

Local, offline, no cloud identity provider, no account that leaves the machine. **The engine owns
authentication and authorisation**; the shell and renderer enforce nothing the engine does not. An
operator account is *separate from the Windows login*, because the aim is to attribute actions and
gate the console, not to replace OS accounts.

### 7.2 First launch and setup

- On first start the engine reports `setup_required: true` and exposes only the setup endpoint.
- **There is no default password and no backdoor account.**
- **Setup claim race.** On a shared machine another local process could reach the loopback port
  first. The supervisor therefore generates a one-time **setup token**, passes it to the engine in
  its environment and to the wizard through the preload bridge; setup is refused without it, and it
  is invalidated on first use. Run without the shell, the token is printed once on the engine's
  console. This is decision 5 in §13.3.
- The wizard then creates the first Administrator and shows the recovery codes (§7.6).

### 7.3 Passwords

- **Argon2id** through `argon2-cffi` (MIT), a per-user random salt, and the parameters stored with
  each hash so they can be raised later with transparent rehash at login. Starting point: 64 MiB
  memory, 3 iterations, 1 lane, **tuned on this machine to about 250 ms per hash in D0 and
  recorded**.
- Minimum length 12, no composition rules, maximum 128. Reject passwords on a **local** common-
  password list (a bundled file, hashed lookups, no network call, licence of the list recorded) and
  any password containing the username. No hints, no security questions.

### 7.4 Login screen

Username and password, a show or hide control, a caps-lock warning, and one generic failure message
("That username and password do not match"), identical for an unknown user and a wrong password.
To keep the response identical in *time*, an unknown user still runs a dummy Argon2 verification.

**Progressive lockout.** After 5 consecutive failures the key is locked for 30 s, doubling to a
15 min cap, reset on success. The counter is keyed by the *lower-cased submitted username* whether
or not that account exists, so the lockout does not reveal which users exist. The remaining wait is
shown ("Try again in 24 s").

**"Remember this device": not offered.** It would persist a credential on a security console that
is meant to lock. Windows Hello as a convenience unlock is a D3 candidate and is not promised.

### 7.5 Sessions and lock

- Server-side session records; the cookie holds only a 256-bit random id, stored as its SHA-256.
- Cookie: `HttpOnly`, `SameSite=Strict`, path `/`, and `Secure` if Chromium accepts it for the
  loopback origin (**to verify in D0**; if not, the loopback origin plus `SameSite=Strict` is the
  protection and this is stated).
- **Idle timeout** 15 min (configurable, 5 min to 8 h) and an **absolute** lifetime of 12 h.
- **Lock console:** a server-side flag on the session. While locked, data and stream endpoints
  return 423 and the UI shows only a password prompt. **Monitoring continues underneath** because
  the engine does not depend on a session. A new CRITICAL still raises a native notification while
  locked, carrying severity and camera only and never an image (to confirm, decision 7).
- **Logout** deletes the session server-side; idle expiry does the same.
- **CSRF:** `SameSite=Strict`, a required custom header on state changes, and the Origin check of
  §5.3. WebSocket upgrades require the cookie and a matching Origin.
- **Session-aware streams:** `stream.mjpeg` and evidence downloads authorise by session cookie, so
  live video works with auth on. The bearer-token path stays for programmatic clients, is
  administrator-issued and revocable, and is never accepted for human sign-in.

### 7.6 Recovery without email

At setup the engine generates **10 one-time recovery codes** (80 bits each), stored as Argon2id
hashes, shown once with a save or print dialog. A code resets one user's password and is consumed.
An Administrator can reset any other user, who must change it at next login. A reset revokes the
account's sessions.

**The unrecoverable case, stated plainly.** If the only Administrator loses the password *and*
every recovery code, there is no network service to ask. A locally stored system cannot be both
secure against someone holding the disk and recoverable by someone who has lost every secret. The
last-resort path is `vigil auth reset-admin`, run from a prompt on the machine as the user who owns the data
directory. It removes the administrator accounts, keeps all incidents and evidence, writes an audit entry, and
requires typing a confirmation phrase. **Anyone who can run it already holds the disk**, because the
account database is the user's own file, which is the boundary §7.9 concedes anyway. That trade is shown in the product's
Help, not hidden.

### 7.7 Roles and enforcement

| Permission | Administrator | Operator | Viewer |
|---|:---:|:---:|:---:|
| View live wall, incidents, evidence, analytics | ✔ | ✔ | ✔ |
| Acknowledge, dismiss, annotate, escalate, resolve | ✔ | ✔ | — |
| Create and cancel mutes | ✔ | ✔ | — |
| Export reports and evidence | ✔ | ✔ | — |
| Edit cameras, zones, modules, settings | ✔ | — | — |
| Manage users and roles, view the audit log | ✔ | — | — |
| Diagnostics bundle, logs viewer | ✔ | — | — |

**Enforced in the engine, not by hiding buttons.** Each route declares one permission through a
FastAPI dependency. A test enumerates `app.routes` and **fails the build if any route lacks a
declared permission**, so a new endpoint cannot ship unprotected by accident. A Viewer calling
`POST /incidents/{id}/acknowledge` gets 403; the UI additionally disables the control with the
reason (08 §8). The last Administrator cannot be deleted or demoted.

### 7.8 Audit log

An append-only table in a **separate database file** from incidents, so incident retention or a
vacuum cannot touch it. It records login success, failure and lockout, logout, expiry, lock and
unlock, role changes, user creation and reset, settings, camera and zone changes (before and
after), mute create and cancel, every incident action, every export and every diagnostics bundle.
Each row stores user, time, action, target and a SHA-256 chain over the previous row. That makes
tampering **evident**, not impossible: someone with the file can rewrite the whole chain. It is
viewable and exportable (CSV, JSON) by Administrators, and `incident_transitions.actor` finally
carries a user id.

### 7.9 Threat model, one page

**What login protects against.** A colleague or visitor at an unattended workstation reading
footage or acknowledging, dismissing or muting incidents; an Operator exceeding their role; an
anonymous process or web page reaching the loopback API (cookie, Origin and Host checks); one user
acting under another's name (audit attribution); guessing (Argon2id and lockout).

**What it does not protect against.**

| Attacker | Why login cannot help |
|---|---|
| An administrator on the OS | Can read process memory, the database, the evidence folder and run `vigil auth reset-admin` |
| Anyone with the disk, offline | SQLite and evidence are **not encrypted**. BitLocker is the correct control and is outside this product |
| Malware running as the logged-in user | Can read the user's files and the cookie jar |
| Someone at an unlocked session where the console was not locked | The idle timeout bounds this; it does not remove it |
| Someone who stole the recovery codes | Codes are a credential; the product says to store them like one |
| A network attacker | Out of scope: the engine binds loopback and refuses other binds without auth (`config/schema/api.py`) |

**Explicitly not claimed:** tamper-proof logs, protection from the machine's owner, evidence
confidentiality at rest, or any anti-forensic property. Encrypting evidence at rest is a possible
D3 item and would have to answer key management first, which this document does not pretend to have
done.

---

## 8. Missing screens, workflows and the screen map

### 8.1 Audit of the current viewer

**Ran:** the embedded viewer was opened in the browser pane against a 4-camera engine and inspected.

| Finding | Evidence | Verdict |
|---|---|---|
| One scrolling page, no routing, no views | `api/viewer.py` | Cannot host 9 workflows |
| Streams, strip, incident list and explanation present and wired to real endpoints | live | Honest; keep the rules, discard the code |
| Landmarks: 4 `header`, **0 `main`, 0 `nav`, 0 `h1`** | DOM query | Fails WCAG 1.3.1 and 2.4.1 structure |
| **No `aria-live` region** | DOM query | A new CRITICAL is not announced to a screen reader |
| No `prefers-reduced-motion` rule, no light theme, no `data-theme` | stylesheet scan | Fails the brief and 08 §7 |
| Image alternatives present ("live stream for cam1") | DOM query | Pass |
| Unnamed buttons: 0; `outline: none` rules: 0 | DOM query | Pass |
| Fixed two-column grid, no layout choice, no expand, no full screen | screenshot | Incomplete |
| No loading, empty or error design beyond text | read | Incomplete |
| No authentication UI | — | Missing |

**Missing workflows:** first-run setup; login and lock; camera add, test and edit; zone drawing;
resolve; search; evidence playback with scrubbing; report preview; settings; users; audit; logs;
diagnostics; notifications; tray.

### 8.2 Screen map

Nine navigation items in a 56 px rail, plus pre-authentication screens and global chrome. Each
screen justifies itself with the 08 §1 test: *what does the operator do differently because of
this?* Brief items that did not earn their own screen are **merged**, and the table says where.

```
 PRE-AUTH (no rail)          AUTHENTICATED SHELL                         GLOBAL CHROME
 ------------------          ----------------------------------------    ------------------------
 Setup wizard                Live Wall (home)   Incident Desk            Status strip (always)
   admin > recovery codes    Timeline           Cameras                  Armed state, camera
   > GPU/CPU check           Zones              Analytics                count, GPU, FPS, p95,
   > model download          System             Administration (admin)   mute countdown, clock
   > first camera            Settings                                    Lock, user menu
   > first zone                                                          Command palette (Ctrl+K)
   > notification test      Login                                        Shortcut sheet (?)
                            Locked console
```

| Screen | Purpose | Earns its place because | Absorbs from the brief |
|---|---|---|---|
| **Setup wizard** | First run, once | The engine cannot be used without an admin; GPU and model problems are best met here | Wizard |
| **Login, locked console** | Gate | Attribution and a stream that authenticates | Login |
| **Live Wall** | Watch | Home screen. A new CRITICAL is never more than one action away from here | **Dashboard** (counts live in the status strip, the incident side rail and System; 08 §1 rejects a metrics dashboard), **Camera Detail** expanded view |
| **Incident Desk** | Act | Master-detail: queue left, evidence and the explanation side by side, keyboard triage | **Incident Detail** |
| **Timeline** | Understand | Health events on the incident axis answer "the camera was reconnecting when that fired" (08 §3) | |
| **Cameras** | Diagnose and configure | Add wizard, connection test, credentials, per-camera modules tab with capability reasons | **Camera Detail**, **Modules & Capabilities** |
| **Zones** | Configure | Polygon editor on a live still, schedules, history | |
| **Analytics** | Review | Trends, dismissal rate per module version, report builder with preview and export | **Reports** |
| **System** | Trust | Tabs: Health, Logs, Diagnostics bundle. Stage latencies, queues, GPU, storage, worker state | **System Health**, **Logs** |
| **Administration** (admin) | Control | Tabs: Users and roles, Audit log | **Users & Roles**, **Audit Log** |
| **Settings** | Preferences | General, detection and models, storage and retention, **notifications and alert rules**, startup, appearance, import or export, About | **Alerts & Notifications**, **About** |

**Two things the brief lists that are deliberately not screens:** a separate *Dashboard* (it would
duplicate the status strip and compete with the wall) and a separate *Alerts & Notifications*
history (alert history is incident history and lives on the Timeline and Incident Desk).

### 8.3 Design system

Tokens, craft rules and the motion budget from 08 §6 to §7 are **kept**, with four changes:

1. **A light theme ships**, not only a token promise: `[data-theme="light"]` with severity hues
   retuned per theme, contrast verified by a script over `tokens.css` in the gate (08 §9).
2. **Severity is icon, label and colour, always.** INFO, LOW, MODERATE, HIGH, CRITICAL each get a
   distinct glyph shape (not only a hue) and a text label. The same component renders it everywhere.
3. **One component library**, built once and reused: Button, IconButton (accessible name
   required by type), Field, Select, Dialog, Drawer, Tabs, Table (windowed), Toast, Banner,
   SeverityBadge, StatusDot, Readout (mono, tabular), Tile, PolygonEditor, Timeline lane, EmptyState,
   ErrorState, Skeleton-less LoadingText.
4. **Target size:** WCAG 2.2 requires 24 px (2.5.8); 08's 32 px for destructive actions is kept.

**Every state is designed, with one rendering rule each:**

| State | Rendering |
|---|---|
| Loading | Determinate text ("loading 1,284 incidents"), no shimmer (08 §8) |
| Empty | Quiet mono text: "NO OPEN INCIDENTS". No illustration |
| Partial | The part that arrived, plus a banner naming what did not |
| Error | What failed, why if known, the control to retry, and the user's input preserved |
| Offline camera | Tile keeps its place, labelled OFFLINE with the reason and time since last frame |
| Reconnecting, degraded | Tile labelled in words with the measured fps against expected |
| Analysis suspended | "ANALYSIS SUSPENDED — degradation step N", persistent label |
| Capability unavailable | Greyed, with the missing capability and the fix, e.g. "needs GROUND_PLANE — calibrate in Zones" |
| Not in this build | Fire, smoke and unimplemented event types are **absent** from filters and charts, and named once in Cameras → Modules as "not available in this build" |
| Permission denied | Control disabled with "Requires Operator role", never enabled-then-error |
| Read-only role | Viewer sees data with action controls disabled and explained |
| Engine not responding | Full-width banner, a restart control, last-known data marked stale |
| Locked | Password prompt only; monitoring status line stays |

### 8.4 Operator ergonomics

- **Keyboard-first triage** on the Incident Desk: `j` and `k` move, `a` acknowledge, `d` dismiss,
  `e` escalate, `n` annotate, `r` resolve, `/` search, `?` shortcut sheet. Shortcuts are visible in
  tooltips and in the sheet. They never fire while a text field has focus.
- **Command palette** (`Ctrl+K`): jump to a view, camera or incident, run a permitted action.
- **Nothing animates over a live feed.** Motion follows 08 §7: a single 150 ms entry for a new
  incident, a three-cycle border pulse for the first unacknowledged CRITICAL, then static.
- **The most recent CRITICAL is one action away from anywhere**: the status strip carries an
  incident chip that opens it on the Incident Desk.
- **Density without clutter:** 13 px base, hairlines not shadows, twelve incidents visible without
  scrolling (08 §6).

### 8.5 Accessibility, WCAG 2.2 AA

Semantic landmarks and one `h1` per view; accessible names for every control and icon; visible
focus rings (never `outline: none`); full keyboard operation including the wall, the incident
list and the **zone editor**, which must offer point entry by keyboard and numeric fields because
2.5.7 forbids a drag-only path; 4.5:1 text contrast checked by script; reduced motion honoured
(transitions become opacity-only, the live dot static); 200 % zoom and 400 % reflow without loss;
no information by colour alone; an `aria-live="assertive"` region announces a new CRITICAL once,
not on every poll; focus never hidden behind a sticky bar (2.4.11). Automated checks use axe-core
at the WCAG 2.2 AA tag set plus keyboard-only Playwright journeys. **Automation finds a minority
of accessibility defects**; a manual screen-reader pass with NVDA is part of D1's exit and its
result is reported as manual.

### 8.6 Workflow audit — what works end to end today

"Ran" means executed in this audit against a live engine. Statuses: **working**, **broken**,
**incomplete**, **missing**, **unverified**.

| # | Workflow | Status | Evidence |
|---|---|---|---|
| 1 | Startup and initialisation | **Working** (CLI only) | `vigil run --config-dir …` on the webcam and on 4 synthetic cameras; selftest 60/60 in the gate. **Refuses zero cameras (F1)** |
| 2 | First-time setup, admin creation | **Missing** | no accounts code anywhere in `src/` |
| 3 | Login, lockout, logout, expiry | **Missing** | only the static bearer token in `api/security.py` |
| 4 | Adding and connecting a camera | **Incomplete** | a YAML file plus a restart; webcam connects (ran); RTSP reports FAILED; no API or UI |
| 5 | Live monitoring | **Working, limited** | 4 tiles rendered in the browser; overlays boxes, labels, info only; fixed grid |
| 6 | Inference | **Working** | `yolox_s`, CUDA, p50 36 ms, 10.0 fps on the webcam |
| 7 | Tracking | **Working** (tests; live track dump) | `test_tracker.py`; evidence includes `track_dump` |
| 8 | Zone and event evaluation | **Working** on synthetic scenes; **partly unverified live** | whole-frame zone loaded and `ZONES` granted (ran); `person_down` fired; **`intrusion` produced no candidate** in the live run |
| 9 | Temporal confirmation | **Working** | candidate promoted with 7 severity factors and an accumulator trace |
| 10 | Incident creation | **Working** | `INC-20261102-2C2WRV`, severity MODERATE, confidence 0.9997 (**saturated — a known tuning finding, not evidence of correctness**) |
| 11 | Evidence capture | **Working** | 7 items; `clip_post.mp4` 390,925 bytes; `x-evidence-integrity: ok` |
| 12 | Showing the incident to the operator | **Working in the viewer**, no push | the list polls; no toast, sound or notification |
| 13 | Reviewing evidence | **Working in the API**; playback UI unverified | range requests served |
| 14 | Acknowledge and resolve | **Acknowledge working** (`confirmed → active`, actor `operator`); **resolve missing** as an operator action | ran |
| 15 | Analytics | **Working** | `/analytics/summary` counted the incident and reported `covers_full_window: false` |
| 16 | Report export | **Working** (CSV, JSON, PDF with extra) | `unit/analytics/test_export.py`, `test_pdf.py`; not re-run live |
| 17 | Restart with persisted data intact | **Working** | after restart: 1 incident, `active`, `persists_incidents: true`, schema version 1 |
| 18 | Camera disconnect and recovery | **Unverified live** | unit-tested only; **no unplug drill was run** (the user's camera was not touched) |
| 19 | Missing model; missing GPU | **Partly verified** | `vigil doctor` runs in the gate and on a bare install (ADR 0015 §D); a deleted model and a driver reset were **not drilled** |
| 20 | Run from the tray in the background | **Missing** | no shell |
| 21 | Clean shutdown that releases the camera | **Working** | 515 ms and 496 ms clean; the camera reopened on the next start |
| 22 | Role enforcement for a Viewer | **Missing** | no roles |

**Defects found by running it** (not in the unit suite): F7 warm-up DEGRADED on every start; F8
stream rate 8.1–8.9 fps against a configured 10; overlay options narrower than 08.

---

## 9. Reliability, security and performance gaps

### 9.1 Reliability

| Gap | State |
|---|---|
| Worker restart and degradation ladder | Not built (01 §9). **Detected, not restarted** |
| Engine process supervision | Does not exist; the shell provides it (§5.2) |
| F7 warm-up DEGRADED and F8 stream rate | Open defects, found in this audit |
| A blocked capture read cannot be cancelled | OpenCV `read()` has no timeout (ADR 0011). **Worse for RTSP**, where a dead network leaves a thread blocked until the OS gives up. RTSP needs a library with a real read timeout or a capture subprocess |
| Pre-roll gap annotation, `stream_interrupted` | Not built (07 §5) |
| Soak and failure drills | **None run.** Longest run is 120 s. D4 runs an 8 h synthetic soak and the five drills; a 24 h real-camera soak needs hardware and time that are not available to this build |
| Engine startup time | **Not measured.** It sets the supervisor's restart cost and the first-frame budget in the D0 spike |
| Database maintenance | WAL and integrity check exist; no vacuum, backup or size alert |
| Crash reporting | None. D1 adds a local crash record and the diagnostics bundle; **no telemetry leaves the machine** |

### 9.2 Security

| Gap | State |
|---|---|
| No users, roles, sessions or audit identity | §7 |
| Stream cannot authenticate in a browser | fixed by session cookie |
| No CSRF or Origin and Host checks | needed once cookies exist (§5.3) |
| No WebSocket authorisation | designed in §7.5 |
| Secrets from environment only | `keyring` for camera passwords (D2) |
| Data at rest not encrypted | stated in the threat model; BitLocker is the control |
| Model weights are trust-on-first-use | recorded SHA-256, verified on every load; upstream publishes none |
| Update channel | does not exist; needs a signing key and a certificate decision |
| Third-party notices | no `THIRD-PARTY-NOTICES` file; must exist before an installer ships |
| **Licence** | **none declared** (§13.3 decision 1) |
| Webhook | operator-configured URL only, no credentials in the URL; keep |

### 9.3 Performance and resources

| Measure | Value | Source |
|---|---|---|
| Inference ceiling | about 52 inferences/s; 4 cameras at 10 fps served in full | `docs/PERFORMANCE.md` |
| Engine RSS, 4 cameras, YOLOX-S, idle of viewers | 914 MB | **ran** |
| Engine CPU, same | 4.3 % of the machine | **ran** |
| Webcam inference p50 | 36 ms at 10 fps (clocks idle low at low duty cycle, ADR 0012 A7) | **ran** |
| **VRAM held by other applications** | about 4.9 of 6 GB | **ran** (`nvidia-smi`); leaves roughly 1.2 GB for the detector and any Chromium GPU process |
| **NVIDIA runtime libraries** | **2,457 MB unpacked** in the environment (cuDNN 1,096, cuBLAS 737, cuFFT 275, NVRTC 179, nvJitLink 84, cuRAND 78) | **ran** `du`; ONNX Runtime itself 363 MB, OpenCV 113 MB |
| Model files | 57 MB for three YOLOX sizes | **ran** |

The 2,457 MB figure decides the installer: shipping the CUDA wheels inside it makes a multi-gigabyte
download for a CPU-only user and bundles proprietary libraries by default. The plan therefore
ships a **CPU engine in the installer** and offers the **GPU runtime as an explicit, hash-verified
download** (§13.3 decision 2), which also matches the existing rule that models are fetched
explicitly and never bundled silently.

---

## 10. Prioritised feature matrix

Phases are **D0** foundation, **D1** core desktop, **D2** operational completeness, **D3**
advanced, **D4** production readiness. Complexity: Low, Medium, High, Very High.

### 10.1 In the roadmap

| Feature | Current status | Phase | Dependencies | Complexity | Reason |
|---|---|---|---|---|---|
| Superseding ADR (accounts); licence ADR | None | D0 | decisions 1, 6 | Low | Blocks auth and any installer |
| `vigil serve`, zero-camera start, ready handshake | **NI** (F1) | D0 | — | Medium | Everything else needs an engine that starts empty |
| Frontend gates (tsc, eslint with jsx-a11y, vitest, Playwright, axe, 500-line rule, contrast script, generated types) | None | D0 | — | Medium | Part B rule 7; cannot be retrofitted |
| Electron shell skeleton and measured spike | None | D0 | `vigil serve` | Medium | Can reverse the architecture; measures what §5 only reasoned |
| Auth core: store, Argon2id, sessions, roles, lockout, audit, recovery | **NI** | D0 | ADR 0016 | High | Security model; route-permission test |
| Actor through `IncidentActions` and mutes | **RI** (F3) | D0 | auth core | Low | Attribution |
| Fix F7 warm-up DEGRADED; investigate F8 | Open defects | D0 | — | Medium | Affects detection and reliability data |
| WebSocket telemetry hub | **NI** (F4) | D1 | auth core | Medium | Live indicators, toasts |
| Console shell, design system, Login, Setup wizard (admin only) | **NI** | D1 | gates, auth core | High | The product's face |
| Live Wall with layouts, expand, full screen, overlay toggles | **PI** | D1 | WebSocket, F6 overlays | Medium | Primary view |
| `tracks`, `trails`, `zones` overlays | **NI** (F6) | D1 | — | Medium | Brief C |
| Incident Desk, detail, evidence playback, integrity badge, explanation | **PI** (API only) | D1 | — | High | Primary workflow |
| Operator **resolve** action and search | **NI** | D1 | lifecycle review | Medium | Brief F |
| Tray, single instance, notifications, audio, minimise to tray | **NI** | D1 | WebSocket | Medium | Brief H, L |
| Process supervisor with backoff; engine worker restart | **NI** | D1 | `vigil serve` | High | Brief Part B; 01 §9 |
| System view: health, logs viewer, storage | **PI** | D1 | — | Medium | Brief K |
| Camera add, edit, remove, enumerate, connection test | **NI** | D2 | `CameraManager`, config writer | High | Brief B |
| `keyring` camera credentials | **NI** | D2 | `CameraManager` | Low | Rule: credentials never logged |
| RTSP (TCP, timeout) | **NI** | D2 | library spike | High | Brief B; hang risk |
| Video-file replay, PACED | **NI** | D2 | — | Medium | 07 §2; also the test fixture path |
| Zone editor with live apply and capability re-grant | **NI** (F2) | D2 | `CameraManager` | Very High | The one change that touches `pipeline/assembly.py` |
| Alert rules, quiet hours, per-camera, severity mapping | **NI** | D2 | WebSocket | Medium | Brief H |
| Users and roles UI; audit log UI | **NI** | D2 | auth core | Medium | Brief M |
| Analytics and report UI | **PI** (API only) | D2 | — | Medium | Brief I |
| Degradation ladder | **NI** | D2 | worker restart | High | 01 §9; defined by measurement, not guess |
| Pre-roll gap annotation, `stream_interrupted` | **NI** | D2 | — | Medium | 07 §5; operator trust in evidence |
| Settings, config import and export, restore defaults | **NI** | D2 | config writer | Medium | Brief J |
| Diagnostics bundle | **NI** | D2 | redaction (exists) | Low | Brief Part B |
| Full first-run wizard (GPU, model, camera, zone, notification test) | **NI** | D2 | camera and zone work | Medium | Brief Part B |
| Timeline view; candidate-rejection feed | **PI** (API only) | D3 | WebSocket | Medium | 08 §3 |
| Command palette; shortcut sheet | **NI** | D3 | — | Low | Ergonomics |
| False-positive review loop; structured dismiss reasons | **PI** | D3 | — | Medium | Produces the only honest measurement available |
| `crossing_line` zone and a line-crossing module | **NI** | D3 | zone editor | High | Brief E; geometry only, labelled unevaluated |
| Single-incident evidence bundle (zip) | **NI** | D3 | — | Medium | Export |
| Per-camera threshold overrides | **NI** | D3 | settings | Medium | Brief D |
| WebSocket binary frames with client overlays | **NI** | D3 | measurement | High | Only if server overlays prove limiting |
| Windows Hello unlock; evidence encryption | **NI** | D3 | key management design | Very High | Not promised |
| Installer, uninstaller, per-user data dir, start-menu, autostart | **NI** | D4 | licence, decision 2 | High | Brief Part B |
| Signed installer and signed updater | **BL** | D4 | decisions 3, 4 | High | Needs certificate and hosting |
| GPU runtime pack, explicit hash-verified download | **NI** | D4 | decision 2 | High | 2.4 GB of libraries |
| Third-party notices, licence report in CI | **NI** | D4 | — | Low | Rule 6 |
| 8 h synthetic soak; five failure drills | **NI** | D4 | — | Medium | Closes P4/P6 exit criteria |
| Clean-machine install test | **NI** | D4 | installer | Medium | Packaging that works only on the dev machine is not packaging |
| Regenerated `docs/PERFORMANCE.md` for the shell | Stale for UI | D4 | — | Low | Rule: sustained 120 s runs, first 30 s against last 30 s |

### 10.2 Blocked, and why — not in the roadmap

| Item | Why it cannot be done honestly here |
|---|---|
| **Fire and smoke detection** | No licensed, evaluated model; every accurate candidate is AGPL or unlicensed (ADR 0015 §A). A colour or flicker heuristic would be presented as perception (rule 5) |
| **Any accuracy figure** (precision, recall, false-positive rate, mAP) | There is no labelled dataset in this repository, and the user has not supplied one. A figure without labels is fabrication |
| **Threshold tuning** | Needs real footage from the real scene, labelled |
| **A calibrated confidence** | Needs outcomes to calibrate against |
| **Tape-measure validation of m/s** | Needs a physical camera, a tape and a person walking it |
| **24 h real-camera soak** | Needs hardware and 24 hours; D4 runs the 8 h synthetic one |
| **RTSP against real IP cameras** | Only a local loopback RTSP source can be tested here (for example an ffmpeg-fed test server). That proves the protocol path, not any vendor's quirks |
| **Signed installer, signed updates** | Need a certificate or key custody and a decision (§13.3) |
| **Behaviour on other GPUs, other cameras, battery power, other OS** | One machine only |
| **Vehicle collision, crowd anomaly, dangerous motion** | No data, no model, no module |
| **Email alerts** | Need an SMTP account; absent until configured |
| **Monitoring with nobody logged in** | A Windows service cannot reliably reach a user's webcam; unverified here (§5.2) |

---

## 11. Development roadmap with exit criteria

Every phase ends with: `scripts/check.ps1` → `ALL GATES PASSED` plus the new frontend gates; the
application **launched and driven** through that phase's journeys with screenshots in light and
dark at 100 % and 200 % zoom and keyboard-only; `docs/LIMITATIONS.md`, the README status table and
an ADR updated; `docs/PERFORMANCE.md` regenerated by `vigil bench report` if anything
performance-relevant changed (runs of at least 120 s, first 30 s against last 30 s, execution
provider verified per run); and a phase report saying what was built, what was verified and how,
and what was not.

**New frontend gates** added to `scripts/check.ps1` (it keeps every existing step, with no floor
lowered): `tsc --noEmit` strict; ESLint with `jsx-a11y`; Prettier check; Vitest and Testing Library
unit and component tests with coverage floors; Playwright end-to-end tests that **launch the real
Electron app**; axe-core at WCAG 2.2 AA on every screen and state; a token-contrast script; a
500-line limit for `.ts` and `.tsx` mirroring `scripts/gates.py`; a "generated API types are
current" check; a licence report over `npm` and `uv` dependencies that fails on AGPL or GPL.

### D0 — Foundation

**Build:** ADR 0016 (accounts) and the licence ADR; `vigil serve` that starts with zero cameras and
emits a ready line; the `auth/` layer (Argon2id, sessions, lockout, roles, recovery codes, hash-
chained audit store in its own file, the route-permission test) with an import-linter contract
placing it below `api/`; the actor threaded through `IncidentActions` and mutes; `ui/` and
`desktop/` scaffolds with every gate above; OpenAPI-generated types; the Electron spike.
**Fix** F7; **investigate** F8.

**Exit:** all gates green including the new ones; a test proving **every route has a declared
permission** and that a Viewer receives 403 on every mutating route; auth tests for lockout timing,
identical unknown-user responses, session expiry, lock, logout invalidation and recovery-code
single use; the spike measured against its three budgets with Tauri evaluated if two are missed;
Argon2id parameters tuned and recorded; engine start time measured; `Secure`-cookie behaviour on
the loopback origin verified. **Not exit:** any screen.

### D1 — Core desktop

**Build:** console shell and design system; Setup wizard (admin and recovery codes) and Login and
Lock; WebSocket telemetry; Live Wall with layouts and the overlay set including tracks, trails and
zones; Incident Desk with evidence playback and the integrity badge, the explanation panel and
keyboard triage; operator resolve and search; tray, single instance, native notifications, audio;
the process supervisor and worker restart; System view with logs; screen-reader announcements.

**Exit:** the 22 workflows of §8.6 re-run, with 1, 2, 4 and 5 working and 18 and 20 verified by an
actual unplug and a real tray run; keyboard-only journeys pass; axe clean at AA on every screen and
state; a manual NVDA pass reported as manual; engine killed mid-run and restarted by the supervisor
with the operator told; a Viewer account unable to act; stream authenticated by cookie with auth on.

### D2 — Operational completeness

**Build:** `CameraManager`, config writer, camera add, edit, test and enumerate; `keyring`;
RTSP and video-file replay; the zone editor with live apply and capability re-grant; alert rules and
quiet hours; users, roles and audit UI; analytics and report UI; the degradation ladder; pre-roll
gap annotation and `stream_interrupted`; settings and config import and export; diagnostics bundle;
the full first-run wizard.

**Exit:** a camera added through the UI and reaching ONLINE with no restart; a zone drawn on a live
still taking effect on a camera that started with none; RTSP verified against a **loopback** source
and **stated as not verified against real cameras**; a diagnostics bundle inspected and shown to
contain no frame and no credential; the degradation ladder defined from a measured overload run and
shown to engage and release; config round-trip proven lossless.

### D3 — Advanced

**Build:** Timeline; candidate feed; command palette; false-positive review loop; line-crossing
zone and module; single-incident bundle; per-camera overrides; optional binary-frame overlays.

**Exit:** each feature labelled implemented or unevaluated on screen; the review loop produces a
dismissal rate per module version from real operator use and the UI still says it is not a
false-positive rate.

### D4 — Production readiness and polish

**Build:** installer and uninstaller, per-user data directory, start-menu entry, optional autostart,
single-instance; GPU runtime pack with recorded SHA-256; signed installer and signed updates (if
decisions 3 and 4 are made); third-party notices; clean-machine install test; 8 h synthetic soak
and the five failure drills (unplug, disk full, model deleted, config corrupted, GPU driver reset),
each documented; regenerated performance document; final report.

**Exit:** installed on a machine with no development tools and driven through the journeys; an
update applied and a **tampered update refused**; uninstall offers to keep or remove user data;
soak RSS and handle count bounded over 8 h; every drill documented with outcome; the final report
states what production readiness does and does not mean here.

---

## 12. Technology stack and licences

Versions marked ● were **looked up in this audit** (package registries); the rest are named from
knowledge and must be pinned and their licence re-read at adoption. Every dependency's licence is
recorded in the licence report from D0.

| Component | Role | Version | Licence |
|---|---|---|---|
| Electron | Shell | 45.2.1 ● | MIT |
| electron-builder | Installer (NSIS) | 26.18.0 ● | MIT |
| electron-updater | Update client | with builder | MIT |
| React | UI | 19.3.1 ● | MIT |
| Vite | Build | 8.3.5 ● | MIT |
| TypeScript, ESLint, `eslint-plugin-jsx-a11y`, Prettier | Language and static gates | pin | Apache-2.0, MIT |
| TanStack Query, Zustand, Zod | State and validation (08 §5) | pin | MIT |
| Vitest | Unit and component tests | 5.0.5 ● | MIT |
| Testing Library | Component tests | pin | MIT |
| Playwright | End-to-end tests launching Electron | 1.64.1 ● | Apache-2.0 |
| `@axe-core/playwright` | Accessibility checks, **development only, not shipped** | 4.14.0 ● | **MPL-2.0** |
| Python engine (existing) | Engine | 3.11 | n/a |
| `argon2-cffi` | Password hashing | 25.1.0 ● | MIT |
| `keyring` | Windows Credential Manager | 25.7.0 ● | MIT |
| WebSocket support for FastAPI (`websockets` or `wsproto`) | Telemetry | pin | BSD-3 / MIT |
| PyInstaller | Freeze the engine (`onedir`) | 6.22.3 ● | **GPL-2.0-or-later with a bootloader exception that permits distributing frozen applications under any licence**; recorded, and it does not make the product GPL |
| A DirectShow and MSMF device-name enumerator | Camera names | verify | verify (a small MIT package is the likely candidate) |
| RTSP: OpenCV's bundled FFmpeg, or PyAV | RTSP capture | spike | OpenCV Apache-2.0 with LGPL FFmpeg; PyAV BSD with LGPL FFmpeg. **LGPL FFmpeg linkage must be checked per build** |
| `onnxruntime-gpu` | Inference | 1.26.0 (pinned) | MIT |
| NVIDIA CUDA, cuDNN, cuBLAS and related wheels | GPU runtime | as pinned | **NVIDIA CUDA EULA: proprietary, redistribution permitted under its terms; never in the default build** |
| YOLOX weights | Detector | `yolox_s` and others | Apache-2.0 |
| `reportlab` | PDF, optional | 4.2+ | BSD |
| Common-password list | Password check | verify | verify before bundling |

**Considered and not adopted:** Tauri 2 (Apache-2.0 OR MIT, the fallback), PySide6 6.12.0 (LGPL-3,
GPL or commercial), pywebview 6.2.1 (BSD-3), Nuitka 4.3.4 (Apache-2.0, an alternative to
PyInstaller if its startup or antivirus behaviour is better; compare in D4). No AGPL or GPL code is
linked into the product. Ultralytics stays out.

---

## 13. What a complete VIGIL-88 desktop application is

### 13.1 Definition

A VIGIL-88 desktop application is **complete** when all of the following are true and demonstrated
on a machine other than the development machine:

1. It installs and uninstalls cleanly from a signed installer, runs from a per-user data directory,
   enforces one instance, and offers start-at-login into the tray.
2. First launch walks an operator through an administrator, a GPU or CPU check with the fix command,
   an explicit model download, a first camera, a first zone and a notification test.
3. Local accounts, roles, lockout, sessions, lock, recovery and a hash-chained audit log work, and
   every API route is permission-checked by a test that fails when one is not.
4. Webcam, RTSP and video-file cameras can be added, tested and removed from the UI with
   credentials in the Windows Credential Manager and never in a log.
5. The wall, the incident desk, evidence playback with the integrity badge, the explanation panel,
   zone editing with live apply, analytics, reports, alerts and the system view all work, in light
   and dark, to WCAG 2.2 AA, by keyboard alone, with every state designed.
6. The engine survives its own failures: worker restart, a supervisor that restarts the process
   with backoff and tells the operator, a degradation ladder defined from measurement, and an
   8 h soak with five documented failure drills.
7. Every control either works or says why it cannot. Nothing is fabricated, and a metric with no
   data reads "unavailable".
8. Updates are signed and a tampered update is refused. A diagnostics bundle contains no frame and
   no credential.
9. The licence, third-party notices and the NVIDIA distribution policy are decided and shipped.

**What it is not:** a claim that the detectors are accurate. That is a data problem (§10.2) and no
amount of desktop engineering changes it.

### 13.2 Open technical questions for D0 and D2

1. **Electron versus Tauri**: decided by the D0 spike's three measured budgets.
2. **`Secure` cookie on `http://127.0.0.1`** in Chromium: verify; the fallback is stated in §7.5.
3. **Engine startup time**: unmeasured; sets the restart cost.
4. **RTSP library**: OpenCV's FFmpeg or PyAV; the criterion is a real read timeout that can be
   honoured without closing a capture from another thread (ADR 0011).
5. **Whether a camera can be opened from a service** at all: only matters if "monitor with nobody
   logged in" is wanted.
6. **PyInstaller versus Nuitka** for the engine: antivirus false-positive rate and startup time on a
   clean machine.
7. **F8**: why the stream runs at 8.1–8.9 fps of a configured 10.

### 13.3 Decisions that are yours

None of these is taken silently. Part B does not start the affected work until each is answered.

| # | Decision | Why it is yours | Recommendation |
|---|---|---|---|
| 1 | **The project licence** (`LICENSE` and the `license` field) | It grants rights to others and is reversible only in one direction. No installer can ship without it | A permissive licence such as Apache-2.0 fits every dependency in §12, but proprietary all-rights-reserved is equally possible. Not mine to choose |
| 2 | **NVIDIA runtime policy**: bundle the CUDA libraries (about 2.4 GB), download them on demand with a recorded hash, or ship CPU-only | Redistribution terms and installer size | CPU engine in the installer plus an explicit GPU runtime download |
| 3 | **A code-signing certificate**, and whose name appears as publisher | Costs money; the price was not checked here | Needed for a clean SmartScreen experience and for Authenticode-verified updates |
| 4 | **Update hosting and signing-key custody** | Costs or operational burden; a lost key strands every install | A free release host for a first channel, with a custom signature check if no certificate is bought |
| 5 | **Setup-token model** for the first administrator (shell-issued one-time token) | Changes the security model | Adopt it; the alternative is first-come-first-served on a shared machine |
| 6 | **Reversing 01 §2**: approve local accounts, roles and audit, with ADR 0016 | Reverses a recorded decision | Approve; the plan depends on it |
| 7 | **What a locked console may notify** (severity and camera only, or nothing) | Privacy versus responsiveness | Severity and camera only, never an image |

**Not decisions, but things to know:** a Tauri fallback exists; the Rust toolchain is not installed
here; other applications hold about 4.9 GB of this GPU's 6 GB, so GPU headroom for the console is
small; and working-tree hygiene (`.gitattributes`, the untracked `prompts/`, the `graphify-out/`
folders this audit created) is yours to tidy.

### 13.4 What Part B starts with, if approved

D0 in this order, each step ending green: ADRs and the licence decision; `vigil serve` and its
tests; the auth layer; the frontend gates; the Electron spike and its measurements; F7 and F8. No
screen is built before the spike has spoken.
