# VIGIL-88

A real-time computer-vision incident detection and situational-awareness platform, built
from first principles. Its defining idea is that it reasons **across time**: a frame is
evidence, not a verdict, and incidents come from accumulated, verified evidence rather than
per-frame classification.

## Honest status

**P0–P5 are implemented. The chain runs end to end on a live camera: frames become tracks,
tracks become observations, observations accumulate into verified events, and a verified event
becomes a durable incident with its evidence and its audit trail. P6 (fire/smoke, hardening) is
not started.**

| Works today | Does not exist yet |
|-------------|--------------------|
| Real object detection (YOLOX via ONNX Runtime) on the GPU, with CPU fallback | Fire and smoke detection (needs a classifier and evaluation data) |
| Multi-object tracking, zones, occupancy, dwell and spatial relations | Detector accuracy evaluation — no labelled data, so no mAP, precision or recall anywhere |
| Temporal reasoning: log-odds accumulation with half-life decay and dual-threshold hysteresis | Threshold tuning against real footage; every value is the architecture's default |
| Three event modules — `intrusion`, `person_down`, `abandoned_object` — each with its known false positives written down | The operations console (a provisional viewer exists), RTSP, video files |
| Eleven verification validators and an eight-factor severity score, both explained per incident | Degradation ladder, worker restart, 8-hour soak |
| Durable incidents in SQLite: dedup, lifecycle, operator actions, full transition history | Multi-camera identity, pose estimation, cross-camera calibration |
| Evidence capture with pre-roll and post-roll, hashed, with missing items named rather than faked | |
| Analytics and reports over persisted records; JSON, CSV and (with `--extra reports`) PDF export | |
| Optional four-point ground-plane calibration: velocity in m/s, px/s and labelled px/s without it | |
| Benchmark tooling that measures sustained load; results in [PERFORMANCE.md](docs/PERFORMANCE.md) | |

**What this does not tell you is how well it detects anything.** There is no labelled dataset
here, so there is no measured false-positive or false-negative rate for any module, and a
confidence is a score that orders events sensibly rather than a calibrated probability. See
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
