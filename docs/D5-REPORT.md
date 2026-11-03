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

SECTION-BUILT

## 3. Verification

SECTION-VERIFIED

## 4. What remains

SECTION-REMAINS
