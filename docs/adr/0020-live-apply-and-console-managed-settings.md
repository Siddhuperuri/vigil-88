# 0020: What applies live, where console settings live, and how settings move

- **Status:** accepted
- **Date:** 2026-11-03
- **Supersedes in part:** [0017](0017-apply-configuration-by-controlled-restart.md), which said nothing applies
  live. Most still does not; two things now do.

## Context

ADR 0017 chose to apply every change by a controlled restart, because the engine builds each camera's capture
path, capability set and module bindings once, at start. That stays true. But two kinds of change do not touch
any of that, and making a person restart monitoring to change who gets an alert is a cost with no benefit.

## Decision

**Live:**

1. **Zone and line edits, when the camera's capabilities do not change.** The scene analyzer accepts a swap
   (`SceneAnalyzer.request_swap`). It is handed over under a lock and applied by the analysis thread at the
   start of the next frame, so no frame is analysed against half an edit and the occupancy tracker is only ever
   touched by its own thread. An edit that would add or remove the camera's ZONES capability is **refused live**
   and waits for a restart, with the reason: modules are bound from that capability.
2. **The alert policy** (the on/off switch, the default threshold, rules, quiet hours).
   `AlertDispatcher.reconfigure` swaps one reference. Channels are not rebuilt; adding a channel or changing the
   webhook address still needs a restart.

**Not live, and the console says "applies after a restart":** cameras (added, changed, removed), module
thresholds, the detector, ingest and every other section.

**Where console-managed settings are stored.** The loader already layers `vigil.local.yaml` over `vigil.yaml`.
The console writes there and nowhere else in the main configuration, and **only sections on an allowlist**
(`alerts`). A hand-edited `vigil.yaml` is never rewritten, so a person's comments survive. Every write first
copies the old file to `config/backups/` (pruned to the newest 50).

**Thresholds** per camera are the camera file's `module_params`, validated by binding the module with the merged
values, so the limits shown are the limits enforced and a typo is refused.

**Moving settings.** Export is a versioned JSON document of the alert policy, cameras, zones and shared
thresholds. It carries **no** password, stored credential, webhook address or account. Import is untrusted input:
size-capped, every model refuses unknown keys, a newer format version is refused with a message, everything is
validated before anything is written, a dry run is the default, it adds and replaces but never deletes, it backs up
what it replaces, and a failed write restores the previous files.

## Consequences

- A zone edit and an alert change take effect at once; a camera or threshold change still needs the bar's restart.
- `application.settings` is replaced on a live alert change, so the configuration hash stamped on later incidents
  reflects what is in force.
- The live path is exercised by tests against a running engine (`tests/unit/api/test_config_admin.py`,
  `test_settings_admin.py`). It has not been watched on a long run.

## Rejected

- **Live camera add and remove.** It is a rewrite of the pipeline's assembly (queues, scheduler plans, workers).
- **Writing alert settings into `vigil.yaml`.** It would destroy comments and layout.
- **Import that replaces everything.** One wrong file would delete cameras.
