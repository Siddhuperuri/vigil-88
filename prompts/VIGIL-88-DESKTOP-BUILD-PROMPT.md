# VIGIL-88 — AUDIT, THEN BUILD THE COMPLETE DESKTOP APPLICATION

Act as a principal software architect, senior desktop application engineer, computer-vision
engineer, product designer and security engineer.

VIGIL-88 is at `C:\Projects_AI\Vigil-88` (public repo `github.com/Siddhuperuri/vigil-88`, `master`).
P0–P6 are merged. The goal now is to turn it into a **complete, polished, fully functional native
desktop application** with local user accounts and login, and every feature that can be built
honestly on this codebase and this hardware.

This prompt has two parts. **Part A is an audit and plan and changes no code.** Part B is the
build, and it does not start until I approve the Part A report.

---

## 0. Ground truth you must start from

Read these before anything else. They are authoritative; verify their claims against the code
rather than trusting either.

- `docs/architecture/01..10` — the design set. `08-ui-architecture.md` is the console design.
- `docs/adr/0011..0015` — what each phase decided and deviated from.
- `docs/LIMITATIONS.md` — the honest inventory of what does not exist.
- `docs/P4-P6-FINAL-REPORT.md` — the most recent state of the system.
- `docs/PERFORMANCE.md` — the only place a performance claim may live. Generated, never hand-written.
- `~/.claude/UX_DESIGN_PRINCIPLES.md` — read before any UI work. If it is missing, apply my
  global UX standards and continue.

Facts already established, so you do not rediscover them:

- Python 3.11, `uv`, Windows 11, RTX 3050 **Laptop** 6 GB on AC power. ONNX Runtime 1.26 pinned
  (CUDA 12; later releases need CUDA 13, which has no Windows pip cuBLAS). YOLOX (Apache-2.0).
- The current UI is a **provisional browser viewer** served by FastAPI (`src/vigil/api/viewer*.py`),
  MJPEG streams, loopback by default, optional bearer token. With auth on, a browser `<img>`
  cannot send headers, so the stream breaks for the viewer — **a cookie/session login fixes this**.
- RTSP, video-file replay and image input are **not implemented**; RTSP cameras report FAILED.
- `fire` and `smoke` are **declarations, not detectors**. They require `CLASSIFICATION`, which
  `core/capabilities.py::UNGRANTABLE` records as ungrantable. No licensed, evaluated model exists
  (ADR 0015 §A).
- No degradation ladder, no worker restart (crashes and stalls are detected and reported only),
  no spatial coalesce, no pre-roll gap annotation, no `stream_interrupted` on open incidents.
- 01 §2 and 10-roadmap §10 say **multi-user auth and RBAC are "not planned"**. Login reverses
  that decision, so it needs a superseding ADR, not a silent change.
- **The project declares no licence** (no `LICENSE`, no `license` field). The `onnx-gpu` extra
  pulls proprietary NVIDIA redistributables. Both must be resolved before an installer ships.
- Measured ceiling: about 52 inferences/s; 4 cameras served in full at 10 fps each.
- On this machine the webcam cannot open through MSMF and falls back to DirectShow.
- `docs/LIMITATIONS.md` §9 of the final report: **no labelled dataset exists**, so no module has a
  measured false-positive or false-negative rate, and no threshold has been tuned on real footage.

---

## 1. Non-negotiable rules (both parts)

1. **The engine stays independent of the UI.** Detection, tracking, temporal reasoning,
   verification, severity, incidents and evidence must run with no UI attached, and the UI must
   be replaceable without touching them. The UI runs no inference and owns no business logic.
2. **Modules emit Observations, never incidents (D-003).** The temporal engine's mathematics —
   log-odds accumulation, half-life decay, dual-threshold hysteresis, a 0.5 signal contributing
   exactly zero — does not change.
3. **Capability negotiation (D-008) reports only what a running component grants**, with an
   actionable reason. A disabled control in the UI says why it is disabled.
4. **Nothing is fabricated.** No fake incidents, evidence, analytics, model capabilities, benchmark
   numbers, accuracy figures or test results. A metric with no data is shown as **unavailable**,
   never as zero. No demo data in a shipped build unless it is labelled as demo data on screen.
5. **No heuristic presented as perception.** Fire, smoke, vehicle collision, crowd anomaly or any
   other category is shipped only with a licensed model and a measured precision and recall on a
   held-out set. Otherwise it stays a declaration and the UI says "not available in this build".
6. **Licensing:** no AGPL or GPL code linked into the product. Any detector under a restrictive
   licence stays behind the existing `Detector` abstraction and out of the default build. Record
   the licence of every new dependency.
7. **Do not weaken the quality gate.** `powershell -File scripts/check.ps1` must end with
   `ALL GATES PASSED` at every phase boundary. Do not disable tests, loosen assertions, swallow
   errors, lower coverage floors, or mark unverified work complete. Every source file stays under
   500 lines (`scripts/gates.py`). Add equivalent gates for any new frontend code.
8. **Commits:** small and coherent, conventional messages, **no `Co-Authored-By` or any Claude or
   Anthropic attribution line**. Do not push or merge without my say-so.
9. **Stop and ask** when a decision is genuinely mine: the project licence, an architecture that
   rewrites the engine, anything that costs money, or anything that changes the security model.

---

# PART A — AUDIT AND BLUEPRINT (no code changes)

## A1. Rapid project audit

Inspect the repository, architecture documents, ADRs, source, dependencies, configuration, tests,
UI and entry points. Prioritise implementation files over reading every line.

Evaluate: architecture and modularity; the vision and inference pipeline; camera input and
management; detection and tracking; temporal event processing; incident management and evidence
storage; persistence; the existing UI; API and backend communication; logging, monitoring and
diagnostics; configuration; testing and reliability; dependencies and licensing; build,
deployment and packaging readiness.

**Do not assume a feature exists because a document mentions it.** Classify every important
feature as one of:

`IMPLEMENTED AND VERIFIED` · `IMPLEMENTED BUT UNVERIFIED` · `PARTIALLY IMPLEMENTED` ·
`NOT IMPLEMENTED` · `BLOCKED` · `REQUIRES IMPROVEMENT`

For each, cite the file and the test (or say there is none). Identify architectural
inconsistencies, missing integrations, technical debt, duplicated functionality and failure points.

## A2. Desktop architecture

Compare at least: Tauri (web UI + Rust shell), Electron (web UI + Chromium), PySide6/PyQt6
(native Qt in-process), and any other option you can justify. Score each on:

- compatibility with the existing Python engine and the CV pipeline;
- GPU and ONNX Runtime support, including CUDA DLL loading on Windows;
- memory footprint and startup time; UI performance with 4+ live streams;
- camera and device access; native integration (tray, notifications, file dialogs, autostart);
- packaging and installer complexity on Windows; code signing; auto-update;
- licensing (PySide6 is LGPL; Qt commercial terms; Electron/Tauri are MIT/Apache);
- future cross-platform support; maintainability; how much existing code survives.

**Recommend ONE architecture.** Do not recommend rewriting the engine without a compelling,
measured reason. The preferred shape is a standalone desktop app whose UI talks to the engine over
a defined local boundary (HTTP/WebSocket on loopback, or IPC), so a UI crash cannot take the engine
down and an engine stall cannot freeze the UI. Specify how video reaches the UI (MJPEG, WebRTC,
shared memory, or encoded frames over WebSocket) and measure it, not assume it.

## A3. Feature inventory

For every group below, mark what exists, what is proposed, and what is impossible here and why.

- **A. Dashboard** — system overview, camera count and health, live detection stats, active
  incidents, recent events, severity distribution, CPU/GPU/VRAM/RAM, storage health, app health.
- **B. Camera management** — add/edit/remove; webcam, RTSP, video file, image; connection test;
  auto-reconnect; health; naming and groups; per-camera resolution, FPS, priority and modules;
  live preview; multi-camera; device enumeration by name (indices are unstable on Windows).
- **C. Live monitoring** — camera wall with configurable layouts (1/2×2/3×3/custom); single
  expanded view; full screen; boxes, labels, confidence, track IDs, trails, zone overlays,
  per-overlay toggles; connection and detection status per tile; live event indicators;
  snapshot capture; pause/resume a camera.
- **D. Detection intelligence** — person, vehicle, tracking, intrusion, restricted zones,
  person-down, abandoned object, fire, smoke, and any further signal you can justify. Per-camera
  thresholds, capability indicators, and a false-positive review loop. **Distinguish implemented
  from proposed every time.**
- **E. Zone management** — polygon drawing on a live still, restricted areas, entry/exit lines,
  direction of travel, zone rules and schedules, enable/disable, per-camera zones, zone history.
  Applied without restart (P2 already promised this).
- **F. Incident management** — queue with severity, type, status, acknowledge, dismiss, escalate,
  resolve, notes, timeline, search, filters, evidence preview and playback, the explanation panel
  (observations, accumulator trace, every verdict, severity factors summing to the score), export.
- **G. Evidence** — automatic capture, pre/post-roll, snapshots, playback with scrubbing,
  metadata, SHA-256 integrity verification shown to the operator, retention, storage use, export.
- **H. Alerts and notifications** — in-app toasts, native OS notifications, audio alerts with
  configurable sounds and severity mapping, per-camera rules, quiet hours, mutes with countdown,
  acknowledgement, history; email/webhook only where the channel can honestly report availability.
- **I. Analytics and reports** — trends, by camera, by severity, by hour, detection latency,
  time-to-acknowledge, time-to-resolve, dismissal rate per module version, camera reliability;
  daily/weekly/monthly; CSV, JSON, PDF.
- **J. Settings** — general, cameras, detection, models, GPU/CPU, storage, retention,
  notifications, logging, startup behaviour, performance, import/export config, restore defaults.
- **K. System monitoring** — CPU, GPU, VRAM, RAM, inference latency, FPS, queue depth,
  skipped vs dropped frames, worker heartbeat and stall state, storage, errors, diagnostics bundle.
- **L. Desktop integration** — native window and menu, system tray, minimise to tray, background
  monitoring, start on boot, native notifications, global shortcuts, native file dialogs,
  single-instance lock, crash recovery, logs viewer, local DB management, offline operation,
  installer and uninstaller, signed auto-update, version/about, first-launch setup wizard,
  graceful shutdown that releases the camera.
- **M. Accounts and security (new)** — see A4.

## A4. Login and local accounts

Design local, offline-first authentication for a desktop security product. No cloud identity
provider, no account that leaves the machine.

- **First launch:** a setup wizard creates the first administrator. There is no default password
  and no backdoor account.
- **Login screen:** username and password; show/hide password; caps-lock warning; clear errors
  that do not reveal whether the username exists; progressive lockout after repeated failures,
  with the remaining wait shown; "remember this device" only if justified.
- **Passwords:** Argon2id (or scrypt) with per-user salt and parameters stored with the hash;
  minimum length and a breached/common-password check from a local list, not a network call.
- **Sessions:** short-lived, revocable, HttpOnly + SameSite=Strict cookies if the UI is web-based,
  or an in-memory token over IPC if native. Idle timeout and screen lock ("lock console",
  re-enter password, monitoring keeps running underneath). Logout invalidates server-side.
- **Recovery without email:** printed one-time recovery codes created at setup, plus an
  administrator reset for other users. Document what happens if the only administrator loses
  everything — and say plainly that a locally stored system cannot be made unrecoverable *and*
  recoverable at once.
- **Roles:** at minimum Administrator (everything), Operator (monitor, acknowledge, dismiss,
  annotate, export), Viewer (read-only). Enforce in the engine API, not only by hiding buttons.
- **Audit log:** append-only record of logins, failed logins, lockouts, role changes, settings
  changes, mutes, incident actions and exports, with user and time. Viewable and exportable.
- **Session-aware streams:** the stream endpoint must authorise by session so live video works
  with auth enabled, removing the `<img>` limitation in LIMITATIONS.
- **Threat model:** one page. What login protects against on a shared workstation, and what it
  does not (an attacker with admin rights on the OS, or with the disk).

This reverses 01 §2. Write the superseding ADR as part of the plan.

## A5. UI/UX audit and design system

Audit the current viewer as a professional security-operations console and list missing screens,
incomplete workflows, weak hierarchy, unclear states, missing loading/empty/error states, and
accessibility failures.

Then specify the target:

- **Navigation and screens:** Login, Setup Wizard, Dashboard, Live Wall, Camera Detail, Incident
  Desk, Incident Detail (evidence + explanation), Timeline, Analytics, Reports, Cameras, Zones,
  Modules & Capabilities, Alerts & Notifications, Users & Roles, Audit Log, System Health,
  Settings, Logs, About. Justify every screen; merge any that do not earn their place.
- **Design system:** tokens for colour, type, spacing, radius, elevation and motion; a dark
  operations theme as default and a light theme; a severity scale (INFO→CRITICAL) that never
  relies on colour alone (icon + label + colour); one component library used everywhere.
- **Operator ergonomics:** keyboard-first incident triage (next/previous, acknowledge, dismiss,
  annotate, escalate) with a visible shortcut sheet; command palette; nothing that animates over a
  live feed; information density without clutter; the most recent critical incident never more
  than one action away.
- **Every state designed:** loading, empty, partial, error, offline camera, reconnecting,
  degraded, capability unavailable, permission denied, read-only role.
- **Accessibility:** WCAG 2.2 AA — semantic structure, accessible names, visible focus, full
  keyboard operation, 4.5:1 contrast, reduced-motion respected, 200% zoom without loss, screen
  reader announcements for new critical incidents.

## A6. Workflow audit

Verify by running the application, not by reading code, whether each works end to end:

1. startup and initialisation; 2. first-time setup and admin creation; 3. login, lockout, logout,
session expiry; 4. adding and connecting a camera; 5. live monitoring; 6. inference; 7. tracking;
8. zone and event evaluation; 9. temporal confirmation; 10. incident creation; 11. evidence capture;
12. showing the incident to the operator; 13. reviewing evidence; 14. acknowledging and resolving;
15. analytics; 16. report export; 17. restart with persisted data intact; 18. camera disconnect and
recovery; 19. missing model and missing GPU; 20. running in the background from the tray;
21. clean shutdown that releases the camera; 22. role enforcement for a Viewer account.

Mark each step working, broken, incomplete, missing or unverified, with evidence.

## A7. Prioritised roadmap

Use P0 (foundation) → P1 (core desktop) → P2 (operational completeness) → P3 (advanced) →
P4 (production readiness and polish). For every feature:

| Feature | Current status | Priority | Dependencies | Complexity (Low/Medium/High/Very High) | Reason |
|---|---|---|---|---|---|

Prefer a dependable application over a long list of shallow features. Put anything that cannot be
done honestly here (fire/smoke without a dataset, accuracy claims without labels, 24-hour soak on
real cameras) in a separate "blocked, and why" table instead of the roadmap.

## A8. Part A deliverable

One report, `docs/DESKTOP-AUDIT-AND-PLAN.md`, in this order:

1. Executive summary. 2. Current project health. 3. Implemented vs missing features.
4. Critical architectural findings. 5. Recommended desktop architecture (with the comparison).
6. Complete feature inventory. 7. Accounts and security design. 8. Missing UI screens and
workflows, with the screen map. 9. Reliability, security and performance gaps. 10. Prioritised
feature matrix. 11. Development roadmap with phase exit criteria. 12. Technology stack, with the
licence of every new dependency. 13. Definition of a complete VIGIL-88 desktop application.

Cite real files and modules. No generic advice.

**Then STOP.** Show me the recommended architecture, the screen map, the roadmap and the open
decisions (licence, signing certificate, anything else that is mine), and wait for approval.

---

# PART B — BUILD (only after I approve Part A)

Implement the approved roadmap phase by phase. Each phase ends with:

- `scripts/check.ps1` → `ALL GATES PASSED`, plus the new frontend gates (type-check, lint, unit
  tests, component tests, accessibility checks with axe or equivalent);
- **the application actually launched and driven** through that phase's user journeys, with
  screenshots of the key screens in light and dark, at 100% and 200% zoom, keyboard-only;
- `docs/LIMITATIONS.md`, the README honest-status table and an ADR updated;
- `docs/PERFORMANCE.md` regenerated by `vigil bench report` if anything performance-relevant
  changed — sustained runs of at least 120 s, first 30 s against last 30 s, execution provider
  verified per run;
- a short phase report: what was built, what was verified and how, what was not.

Build expectations:

- **Engine as a service.** The engine runs as its own process (or isolated worker) with a typed,
  versioned local API; the desktop shell supervises it, restarts it with backoff if it dies, and
  shows the operator what happened. This is also where the missing **supervisor/worker restart**
  and **degradation ladder** belong.
- **Cameras:** implement RTSP (TCP transport, socket timeout, credentials only from the secret
  store, never logged) and video-file replay (PACED, lossless, PTS-driven clock) as 07 specifies.
  Use Windows Credential Manager (`keyring`) for camera passwords.
- **Zones:** visual polygon editor on a live still, normalised coordinates, live apply.
- **Packaging:** a signed Windows installer and uninstaller, start-menu entry, optional autostart,
  per-user data directory, single-instance lock, and an update channel that verifies signatures.
  Models are fetched explicitly with recorded SHA-256, never bundled silently.
- **First run:** wizard covering admin account, GPU/CPU detection with the fix command if CUDA is
  missing, model download, first camera, first zone, notification test.
- **Diagnostics:** a one-click diagnostics bundle (versions, config with secrets redacted, logs,
  health, capability table) that never contains frames or credentials.
- **Tests:** keep the engine suite; add UI component tests, end-to-end tests that launch the real
  app, accessibility tests, and auth tests (lockout, session expiry, role enforcement at the API).

## Final deliverable

The 12-section final report format from `docs/P4-P6-FINAL-REPORT.md`, extended to the desktop
build: what was implemented and verified; what changed and why; deviations from the architecture;
what is tested and how; what is not; measured performance; security posture including the auth
model and threat model; what remains unimplemented; real-world validation limits; datasets and
hardware used and not used; the deployment and update story; and **an explicit statement of what
production readiness does and does not mean here**. Launching successfully is not production
readiness, and no accuracy claim may appear without labelled data behind it.

BEGIN WITH PART A.
