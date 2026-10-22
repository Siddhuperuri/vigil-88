# VIGIL-88

A real-time computer-vision incident detection and situational-awareness platform, built
from first principles. Its defining idea is that it reasons **across time**: a frame is
evidence, not a verdict, and incidents come from accumulated, verified evidence rather than
per-frame classification.

## Honest status

**P0–P6 are implemented. The chain runs end to end on a live camera: frames become tracks,
tracks become observations, observations accumulate into verified events, and a verified event
becomes a durable incident with its evidence and its audit trail.**

**P6 shipped no new detection.** It looked for a fire/smoke model it could use and did not find
one, so fire and smoke are *declared absent* rather than quietly missing (ADR 0015 §A). What it
did ship is the result of auditing the running system against its own design: **ten defects that
no unit test could see**, including a camera whose driver overstated its frame rate being held
DEGRADED for ever — which made it reject every candidate and therefore **detect nothing** — and
`vigil run --selftest` silently losing 27 of its 60 frames. All ten are fixed and pinned.

| Works today | Does not exist yet |
|-------------|--------------------|
| Real object detection (YOLOX via ONNX Runtime) on the GPU, with CPU fallback | **Fire and smoke detection.** Both ship as *declarations*: they require a capability nothing here grants, report UNAVAILABLE on every camera with a reason, and refuse to observe rather than reporting silence. No usable model exists — the accurate ones are AGPL-3.0, the permissive one cannot localise and has no stated held-out evaluation. **No heuristic is shipped in their place** (ADR 0015 §A) |
| Multi-object tracking, zones, occupancy, dwell and spatial relations | Detector accuracy evaluation — no labelled data, so no mAP, precision or recall anywhere |
| Temporal reasoning: log-odds accumulation with half-life decay and dual-threshold hysteresis | Threshold tuning against real footage; every value is the architecture's default |
| Three event modules — `intrusion`, `person_down`, `abandoned_object` — each with its known false positives written down | The operations console (a provisional viewer exists), RTSP, video files |
| Eleven verification validators and an eight-factor severity score, both explained per incident | Degradation ladder and automatic worker restart — a crashed *or stalled* worker is now detected and reported, never restarted |
| Durable incidents in SQLite: dedup, lifecycle, operator actions, full transition history | Multi-camera identity, pose estimation, cross-camera calibration |
| Arbitration that actually suppresses: subsume, escalate and merge, with the folded-in event recorded rather than lost | Spatial coalesce (needs `region:` subjects, which no module emits) |
| Camera reliability: escalating reconnect backoff, stall detection for cameras *and* workers, epoch invalidation that keeps place-based evidence and decays it across the gap | Soak testing — the longest sustained run here is 120 s; neither P4's 8-hour nor P6's 24-hour soak was run, and no failure drill was executed |
| A bare install starts and degrades honestly: `vigil doctor` runs with no extras and names what is missing | A declared licence for this project — there is no `LICENSE` file, so by default no rights are granted (ADR 0015 §C5) |
| Evidence capture with pre-roll and post-roll, hashed, with missing items named rather than faked | |
| Analytics and reports over persisted records; JSON, CSV and (with `--extra reports`) PDF export | |
| Optional four-point ground-plane calibration: velocity in m/s, px/s and labelled px/s without it | |
| Benchmark tooling that measures sustained load; results in [PERFORMANCE.md](docs/PERFORMANCE.md) | |
| A measured multi-camera ceiling: four cameras served in full at 10 fps each, saturating near 52 inferences/s | |

**What this does not tell you is how well it detects anything.** There is no labelled dataset
here, so there is no measured false-positive or false-negative rate for any module, no mAP,
precision or recall for the detector, and no threshold anywhere has been tuned against real
footage. A confidence is a score that orders events sensibly rather than a calibrated probability.
Launching successfully is not the same as being production-ready, and this README is not claiming
the latter. See
[docs/LIMITATIONS.md](docs/LIMITATIONS.md) for the full inventory, module by module. Performance
claims exist only in [docs/PERFORMANCE.md](docs/PERFORMANCE.md), which is generated from stored
measurements.

## Quick start

```powershell
uv sync --group dev --extra onnx-gpu --extra api    # or --extra onnx-cpu without an NVIDIA GPU
uv run vigil models fetch yolox_s                   # explicit download; SHA-256 recorded
uv run vigil doctor                                 # environment, GPU, models, detector, capabilities
uv run vigil run --source webcam:0 --detector yolox_s --serve    # then open http://127.0.0.1:8088/
uv sync --extra reports                             # optional: adds PDF export
uv run vigil bench run --detector yolox_s --name my-run          # measure; never guess
powershell -File scripts/check.ps1                  # the full quality gate
```

Operating notes: [docs/OPERATIONS.md](docs/OPERATIONS.md). Configuration reference:
[docs/CONFIG.md](docs/CONFIG.md) (generated).

## Design

The architecture is specified in [docs/architecture/](docs/architecture/README.md): ten
documents covering the engines, data model, event model, camera pipeline, UI, testing and
roadmap. Deviations found while implementing are recorded in
[docs/adr/0011](docs/adr/0011-p0-implementation-deviations.md) (P0),
[docs/adr/0012](docs/adr/0012-p1-vision-decisions.md) (P1),
[docs/adr/0013](docs/adr/0013-p4-persistence-and-evidence-decisions.md) (P4) and
[docs/adr/0014](docs/adr/0014-p5-module-and-analytics-decisions.md) (P5).

Principles enforced by machine, not convention: layered imports (`import-linter`), a pure
domain layer, the API and UI never importing an inference runtime, no reads of real time outside
one clock module, no swallowed exceptions, no credentials or machine paths in the tree
(`scripts/gates.py`).

## Privacy

Local-first. No facial recognition or identity profiling, and none is planned. The API binds
loopback by default and refuses a non-loopback bind without authentication. Model files are
downloaded only by an explicit command, from a fixed list, over HTTPS, and are integrity-checked
on every load.
