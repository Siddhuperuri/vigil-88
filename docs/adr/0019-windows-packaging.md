# 0019: Windows packaging: a frozen CPU engine in a per-user, unsigned installer

- **Status:** accepted for D4; **not for distribution** until plan decisions 1 to 4 are made
- **Date:** 2026-11-03
- **Phase:** D4
- **Evidence:** `docs/DESKTOP-AUDIT-AND-PLAN.md` §9 (packaging), §10, §13.3

## Context

The shell runs the engine as a child process. A person who installs the app has no Python, no `uv` and no
checkout, so the engine has to arrive with the app. The plan names four owner decisions that gate shipping:
the project licence (1), the NVIDIA runtime policy (2), a code-signing certificate (3), and update hosting
and key custody (4). None has been made.

## Decision

Build everything that does not depend on those decisions, and make the dependence explicit:

- **The engine is frozen with PyInstaller (`--onedir`)** from a throwaway environment holding only the CPU
  extras (`capture`, `onnx-cpu`, `api`, `credentials`, `reports`). It is 223 MB, starts in a few seconds, and
  needs nothing installed. `--onedir` rather than `--onefile`, because a one-file build unpacks itself to a
  temporary folder on every start, which is slow and a favourite of antivirus scanners.
- **No NVIDIA runtime is bundled** (decision 2 is open). An installed engine runs on the CPU. The 2.4 GB GPU
  runtime as an explicit, hash-verified download is **not built**.
- **The installer is electron-builder NSIS, per user** (`perMachine: false`): no administrator prompt, program
  under the user's profile, data under `%APPDATA%\VIGIL-88`, a Start-menu entry, an uninstall entry. No desktop
  shortcut and no autostart unless the person asks in the app.
- **Uninstall keeps the data.** An interactive uninstall asks whether to remove cameras, incidents, evidence
  and accounts, defaulting to No; a silent uninstall always keeps it. An uninstaller should not delete evidence
  unasked.
- **The installer is unsigned** (decision 3) and `scripts/package.ps1` says so when it finishes. There is no
  update mechanism: an updater without a signing key and a host is exactly the thing that must not be
  improvised, and a tampered-update refusal cannot be tested without one.
- **Notices are generated** from the frozen engine's package metadata (`scripts/notices.py`), with the things
  metadata does not show written beside them: the FFmpeg library inside OpenCV (LGPL, a separate DLL), the
  PyInstaller bootloader exception, and the absence of a project licence.

## Consequences

- The installer exists and works on the machine it was built on. It must not be handed to anyone: without a
  licence it grants no rights, and without a signature Windows SmartScreen will warn.
- The packaged app has been run only on the development machine. "Installs on a machine with no development
  tools" is **not demonstrated**: there is no clean machine or virtual machine here.
- Antivirus behaviour of a frozen engine was not measured. PyInstaller output is a known false-positive source.
- Nuitka, the alternative named in the plan, was not compared.

## Rejected alternatives

- **Shipping an embeddable Python and a wheelhouse.** More moving parts to get wrong on a user's machine, and
  the frozen engine is one folder.
- **Bundling the CUDA libraries.** Redistribution terms and a multi-gigabyte installer are the owner's call.
- **A self-signed certificate "so it looks signed".** It would teach users to click through the same warning a
  forged build would show, which is worse than saying nothing is signed.
