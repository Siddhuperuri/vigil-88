# D5 report: closing the gaps

- **Date:** 2026-11-03
- **Branch:** `feat/d5-closeout` (local; not pushed, not merged)
- **Decisions:** [ADR 0020](adr/0020-live-apply-and-console-managed-settings.md),
  [0021](adr/0021-degradation-ladder-and-worker-restart.md), [0022](adr/0022-windows-hello-needs-a-decision.md)

**The four owner decisions are still open, so nothing here is distributable.** What changed is that the
repository now refuses to let a build pretend otherwise, and that the features that were cut are built.
Everything that needs hardware or a clean machine has a harness and a procedure and is marked *not run*.

## 1. The four blocked decisions

| # | Decision needed | Prepared | Cannot be done without |
|---|---|---|---|
| 1 | Which licence | A check that wants the SPDX id, a `LICENSE` file and the same id in `pyproject.toml` | The choice |
| 2 | CPU-only, or an approved NVIDIA download | A downloader that refuses unless the manifest is complete (https, size, SHA-256, a named person's licence review) and `--yes` is given; stops at the recorded size; verifies before keeping | The choice, and NVIDIA's terms |
| 3 | A signing certificate and its custody | `package.ps1 -Release` verifies Authenticode status and subject on the built installer | The certificate |
| 4 | Update host and key custody | A guard: an updater dependency before the decision is itself a blocker | The host and the key |

`release/decisions.yaml` holds the answers; `scripts/release_check.py` reads them and the evidence. Installers are
named `VIGIL-88-DEV-ONLY-Setup-*`, their Start-menu and uninstall entries say *(development build)*, and
`.github/workflows/release.yml` (manual) fails while anything is open. [RELEASE](RELEASE.md) is the procedure.

## 2. What was built

| Item | What it does | Where | State |
|---|---|---|---|
| Pre-roll gap annotation | A hole in the footage (a reconnect, or silence longer than `evidence.gap_threshold_ms`) is recorded and written into the evidence manifest and the incident detail, with any pre-roll shortfall; the console says "this recording has a gap" | `ingest/buffers.py`, `pipeline/preroll.py`, `pipeline/evidence_capture.py`, `response/evidence.py` | Tested; the simulated unplug drill passes |
| Degradation ladder | Three steps with a stated reason, sustained-pressure escalation, slower recovery, a banner on every screen (ADR 0021) | `pipeline/degradation.py`, `pipeline/scheduler.py` | Tested as a pure state machine and in a real application; **thresholds unmeasured** |
| Worker restart | Per worker, administrator only, audited; refuses what cannot be done safely and says why (ADR 0021) | `pipeline/supervision.py`, `pipeline/restartables.py`, `/system/workers` | Tested against a real application and in the real app |
| Line crossing | `lines:` in the zone file, a `CrossingLine` domain type, a `line_crossing` module reading direction from the track's own path | `config/schema/zones.py`, `domain/scene.py`, `events/builtin/line_crossing/` | Synthetic scenes only; no counting |
| Per-camera thresholds | Shown with the module's own bounds, validated by binding the module, saved on the camera | `api/routers/tuning.py`, `Tuning.tsx` | Applies on restart |
| Alert rules and quiet hours | Per camera and per event type; CRITICAL cannot be held; the incident is always recorded | `response/alert_policy.py`, `config/schema/alerts.py` | Applies live |
| Live apply | Zone and line edits when capabilities do not change; the alert policy (ADR 0020) | `api/live_apply.py`, `scene/analyzer.py` | Cameras and thresholds still need a restart |
| Diagnostics bundle | Versions, health, redacted config, counters, log tail; never frames, evidence, accounts, credentials or absolute paths | `api/diagnostics.py` | Tested by planting secrets in config, logs and paths |
| Settings export and import | Versioned; treats the file as untrusted; dry run by default; backup and rollback; no secrets | `api/settings_io.py`, `api/routers/settings_admin.py` | Tested, including a failed write |
| Setup guide | Five steps that use the existing screens; nothing applies until the last | `Wizard.tsx` | Walked in tests and in the real app; not by a first-time user |
| Windows Hello | **Not built.** A security-model decision is needed first (ADR 0022) | | |

Also: `/REMOVEDATA` on the uninstaller, so the remove-my-data branch can be scripted; the installer's Start-menu
and uninstall entries read *(development build)*.

### Defects the work found, and fixed

- Clip muxing ignored the free-space floor: with a nearly full volume every snapshot was refused but the video was
  still written (found by the disk-full drill).
- Restarting a capture worker for an OFFLINE or FAILED camera crashed, because the state machine requires such a
  camera to pass through INITIALIZING.
- A runtime import cycle between the diagnostics module and the system router.
- Zone edits for a camera that had no zones were reported as "nothing needed applying" instead of "waits for a
  restart".
- A suspended camera that reconnected showed ONLINE while the scheduler still was not serving it.
- The system view's new Workers table made an existing end-to-end drill ambiguous (found by running it).


## 3. Verification

GATE-LINE

Run on this machine, with the evidence kept in [verification/evidence](verification/evidence/):

| Check | Result |
|---|---|
| Uninstall, both data branches (`/S` keeps, `/S /REMOVEDATA` removes) | **Both behaved**, on the development installer, on a machine that had never held the data |
| Antivirus: Microsoft Defender scan of the installer and unpacked app, and 30 s of the app running | **No detection.** One engine, one machine; the installer is unsigned; other products untried |
| Install check with only the installer | **Passed, but this is not a clean machine** (Python, Node, uv and git are present). The evidence says so |
| Automated drills: corrupted config, missing model, unreachable camera (real app); disk full, unplug and recovery, gap in footage (real engine, simulated fault) | **Passed** |
| Whole-application soak, **6 minutes**, installed build, every view once a minute | **Journey passed; process tree bounded** (memory +1.2%, handles +0.4%, six processes). 6 of the 480 minutes asked for |
| GPU environment read (`--dry-run`) | A card and CUDA are present. **The benchmark was not run** |

## What was not verified, and what each needs

| Not verified | Needs |
|---|---|
| Install on a **clean** Windows machine | A VM or PC with no development tools; copy the installer and `scripts/verify/clean-install.ps1` |
| The **8-hour** whole-application soak | Eight undisturbed hours: `scripts/verify/soak-app.ps1 -Minutes 480`; real cameras would be better still |
| A **real RTSP camera**, including a network break | A camera, its stored credential, and the ability to interrupt its network: `rtsp_check.py` |
| The **GPU benchmark**, and a driver reset | `gpu_check.py <model>`; a GPU runtime that ships, for the reset |
| **NVDA** | NVDA and a person who uses it ([06](verification/06-nvda.md)) |
| Other antivirus products, SmartScreen | The products, and a downloaded copy of the installer |
| A real full volume, a real unplugged camera | A small virtual disk (administrator), a USB camera |
| The interactive uninstall **prompt** answers | A person at the screen ([08](verification/08-uninstall.md)) |
| The ladder's thresholds, line crossing and gap annotation on **real footage** | Cameras and time; the thresholds were never tuned |


## 4. What remains

### Blockers, in order

1. **Decision 1, licence.** Choose one; add `LICENSE`; record it in `release/decisions.yaml` and `pyproject.toml`.
2. **Decision 2, NVIDIA runtime.** CPU-only, or an approved download with a complete manifest and NVIDIA's terms.
3. **Decision 3, signing.** A certificate, who holds the key, where signing runs.
4. **Decision 4, updates.** A host, an update-signing key and its custody.
5. **Windows Hello** needs the security question in [ADR 0022](adr/0022-windows-hello-needs-a-decision.md) answered.
6. The hardware and clean-machine checks above.

### Follow-ups (recommended, not required)

- Tune the ladder's thresholds from a measured overload run (`vigil bench`) before anyone relies on it.
- Show alerts held by quiet hours on the incident, not only in the log and a counter.
- Put `stream_interrupted` on the incident row so an open incident can be filtered by it.
- Restart a crashed capture worker automatically, bounded by the same circuit breaker.
- Drive the interactive uninstall prompt end to end (it is a message box, so this needs UI automation).

