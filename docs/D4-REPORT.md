# D4 report: packaging, installer, soak and failure drills

- **Date:** 2026-11-03
- **Phase:** D4 of `docs/DESKTOP-AUDIT-AND-PLAN.md`
- **Decision record:** [ADR 0019](adr/0019-windows-packaging.md)

**The installer exists and works on the machine it was built on. It must not be given to anyone.** It is
unsigned (plan decision 3), the project has no declared licence (decision 1), it carries no GPU runtime
(decision 2), and it has no update mechanism (decision 4). None of those four is mine to make.

## What was built

| Item | Where | State |
|---|---|---|
| Frozen CPU engine (PyInstaller `--onedir`, 223 MB) | `scripts/package.ps1`, `scripts/engine_entry.py` | built, starts, `doctor` passes |
| Per-user NSIS installer (no administrator prompt) | `desktop/electron-builder.yml` | built, silent install and uninstall driven |
| Uninstall keeps data unless asked | `desktop/installer/uninstall-data.nsh` | silent path driven; interactive "Yes" branch **not** driven |
| Optional start with Windows, starts hidden in the tray | `desktop/src/autostart.ts`, `StartWithWindows.tsx` | toggled in the real app; registry left clean |
| Single instance, second launch brings the window forward | `desktop/src/main.ts` | covered by e2e |
| Third-party notices from the frozen engine | `scripts/notices.py`, `docs/THIRD-PARTY-NOTICES.md` | generated, flagged rows need a person to read |
| Soak sampler | `scripts/soak.py` | run once, 60 minutes |
| Failure drills | `desktop/e2e/drills.spec.ts` | 3 of 5 run |
| One-command build | `scripts/package.ps1` | venv → freeze → doctor → notices → installer |

## Verified

- The frozen engine runs `doctor` and `serve`. The whole desktop e2e suite (9 tests) passes against it;
  apply-by-restart took 2.5 s against 3.5 s on the source engine.
- The installer installs silently, the installed app passes the packaged-app e2e, and it uninstalls silently
  leaving user data in place.
- **Drills run (3 of 5):**
  1. A corrupted configuration is named on screen with the parser's own words and the offending line; no engine
     is left running; mending the file recovers without restarting the app.
  2. A missing model stops the engine with the command that fixes it (`vigil models fetch yolox_s`) and does not
     pretend to detect.
  3. An unreachable camera is shown as not working (never ONLINE), the engine stays up, and quitting does not
     hang on it. This drill found a real fault: RTSP open and read timeouts of 8 s and 6 s exceeded the 5 s
     shutdown budget, leaving a straggler process. They are now 4 s each, with a test.
- **Soak, 60 minutes, engine only, two synthetic cameras, sampled every 15 s:**

  | Measure | Result |
  |---|---|
  | RSS | 110 MB at the start of the settled period, 112 MB at the end (+1.4%); peak 112 MB |
  | Handles | 367 to 375 |
  | Threads | 30 to 36 |
  | CPU | mean 125.8% of one core |
  | HTTP probe | median 5 ms, worst 79 ms, 0 of 240 unanswered |
  | Shutdown | stopped on request in 329 ms |

  The sampler's own verdict: memory and handles stayed bounded **on this run**.

## Not built, and why

- **GPU runtime pack** (decision 2). The 2.4 GB NVIDIA libraries, as an explicit hash-verified download, are
  not built. An installed engine runs on the CPU.
- **Signing** (decision 3). No certificate, so no signature. SmartScreen will warn. A self-signed certificate
  was rejected on purpose (ADR 0019).
- **Updates** (decision 4). No updater, so "a tampered update is refused" cannot be tested and is not claimed.
- **Licence** (decision 1). Nothing here grants anyone rights to VIGIL-88.

## Not verified

- **No clean machine.** "Installs on a computer with no development tools" is not demonstrated; every
  install ran on the build machine.
- **The soak is 60 minutes, not the 8 hours the plan asks for.** It measures the engine process only: no
  Electron shell, no console, and no detection model, so it says nothing about GPU or model memory. A short
  soak does not show that a long one would pass.
- **Drills not run (2 of 5):** disk full, and unplugging a camera with recovery on replug. The GPU driver reset
  drill needs a GPU runtime that is not shipped.
- **Antivirus behaviour** of the frozen engine was not measured. PyInstaller output is a known false-positive
  source.
- **Nuitka** was not compared.
- The interactive uninstall prompt's "Yes, remove my data" branch was not driven.
- Earlier gaps still stand: a real RTSP camera, the real-GPU spike, a screen-reader pass (NVDA), and applying
  configuration to a live engine without a restart.

## What the owner has to decide

1. The project licence.
2. Whether, and how, the NVIDIA runtime is redistributed.
3. A code-signing certificate and who holds the key.
4. Where updates are hosted and who holds the update-signing key.

Until all four are made, the installer is a development artefact.
