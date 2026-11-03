# 0017: Apply camera and zone changes by a controlled engine restart

- **Status:** accepted for D2; **deviates from the plan's D2 exit criterion**, which asked for live apply
- **Date:** 2026-11-02
- **Phase:** D2
- **Evidence:** `docs/DESKTOP-AUDIT-AND-PLAN.md` finding F2 and §11 (D2 exit criteria)

## Context

The plan said D2 would add a `CameraManager` so a camera could be added, and a zone drawn, with no restart,
including re-granting the `ZONES` capability to a camera that started without zones. Reading the pipeline
for that work found what F2 only suspected. Per-camera state is built once, in `Application._start`, and
is referenced by:

- the inference scheduler's token buckets (`InferenceScheduler(plans)`),
- the inference worker's channel map,
- the analysis assembly (`build_analysis`): one analysis runtime, capability set and module binding report
  per camera, plus the verification ledger and the severity inputs,
- the response stack (pre-roll sampling and evidence capture per camera),
- the stream and results stores.

Changing any camera at run time means making each of those mutable and thread-safe, re-running capability
negotiation and re-binding modules for that camera while the others keep detecting, and proving that no
frame, accumulator or open incident is lost or attributed to the wrong camera. That is a rewrite of
`pipeline/assembly.py` and its neighbours, which the standing rule says to stop and ask about, and it
touches the code that decides what an operator never sees.

## Decision

**A change is saved at once and takes effect after a controlled restart of the engine.**

- The console writes camera and zone files atomically through the same models the loader uses, so a file
  it writes is one the engine will accept (`config/writer.py`).
- `GET /config/pending` reports exactly what differs between the files and the running engine, computed
  from the files, not from a flag, so it is true after a crash and after a hand edit.
- `POST /config/apply` asks the engine to exit. The engine emits `restart_requested`, then `stopped`, and
  exits cleanly; the supervisor relaunches it at once and without the failure penalty. Without a
  supervisor the engine **refuses** (409) rather than exit and stay down.
- The console says so: "Saved. Takes effect when monitoring restarts", with the restart as an explicit
  button, never a surprise. The restart time is measured and recorded in the D2 report: about 3.5 s from the click to the
  camera reading ONLINE, for a video-file camera with no model to load.

## Consequences

- **Monitoring stops for the length of an engine start** (seconds, longer with a model to load) every time
  changes are applied. Every camera is blind for that window, and an event in it is missed. The console
  says this before the person confirms.
- Sessions survive a restart (they are in the account database), so a person is not signed out.
- Nothing in the engine's decision layers changes, so the accumulator mathematics and D-003 are untouched.
- The plan's D2 exit criteria "a camera added with no restart" and "a zone taking effect on a camera that
  started with none" are **not met** and are recorded as such. Live apply remains the right end state and
  needs its own phase: make the per-camera state in the list above a managed collection first.

## Rejected alternatives

- **A `CameraManager` now.** Correct, and a rewrite of the assembly under time pressure, in the one part
  of the system where a subtle bug silences a camera. Rejected for this phase, not for ever.
- **Apply on a timer or on file change.** Restarting monitoring without being asked is worse than asking.
- **Hot-swapping only zones** (`SceneAnalyzer.set_zones` already allows it). It works for a camera that
  already has the `ZONES` capability and not for one that did not, and a control that sometimes applies
  and sometimes silently does not is exactly what the interface rules forbid.
