# VIGIL-88

A real-time computer-vision incident detection and situational-awareness platform, built
from first principles. Its defining idea is that it reasons **across time**: a frame is
evidence, not a verdict, and incidents come from accumulated, verified evidence rather than
per-frame classification.

## Honest status

**P1 (vision and live camera) is implemented. Objects are detected; nothing is tracked or
reasoned about yet, and no incident can be raised.**

| Works today | Does not exist yet |
|-------------|--------------------|
| Real object detection (YOLOX via ONNX Runtime) on the GPU, with CPU fallback | Tracking, scene analysis, zones |
| Strict model loading: manifest + SHA-256, hard memory cap, loud fallback policy | Temporal reasoning, event modules, verification |
| Live annotated MJPEG stream; overlay drawn from the detections on that exact frame | Incidents, evidence, alerts, persistence |
| HTTP API: health, metrics (CPU/GPU/VRAM/latency by stage), cameras, detections, snapshots | The operations console (a provisional viewer exists) |
| Inference scheduler: per-camera rates, global ceiling, priority, no starvation | RTSP, video files |
| Benchmark tooling that measures sustained load; results in [PERFORMANCE.md](docs/PERFORMANCE.md) | Detector accuracy evaluation (no labelled data) |
| Layered config, structured logging with redaction, frozen domain models, capability system | |

See [docs/LIMITATIONS.md](docs/LIMITATIONS.md) for the full inventory. Performance claims exist
only in [docs/PERFORMANCE.md](docs/PERFORMANCE.md), which is generated from stored measurements.

## Quick start

```powershell
uv sync --group dev --extra onnx-gpu --extra api    # or --extra onnx-cpu without an NVIDIA GPU
uv run vigil models fetch yolox_s                   # explicit download; SHA-256 recorded
uv run vigil doctor                                 # environment, GPU, models, detector, capabilities
uv run vigil run --source webcam:0 --detector yolox_s --serve    # then open http://127.0.0.1:8088/
uv run vigil bench run --detector yolox_s --name my-run          # measure; never guess
powershell -File scripts/check.ps1                  # the full quality gate
```

Operating notes: [docs/OPERATIONS.md](docs/OPERATIONS.md). Configuration reference:
[docs/CONFIG.md](docs/CONFIG.md) (generated).

## Design

The architecture is specified in [docs/architecture/](docs/architecture/README.md): ten
documents covering the engines, data model, event model, camera pipeline, UI, testing and
roadmap. Deviations found while implementing are recorded in
[docs/adr/0011](docs/adr/0011-p0-implementation-deviations.md) (P0) and
[docs/adr/0012](docs/adr/0012-p1-vision-decisions.md) (P1).

Principles enforced by machine, not convention: layered imports (`import-linter`), a pure
domain layer, the API and UI never importing an inference runtime, no reads of real time outside
one clock module, no swallowed exceptions, no credentials or machine paths in the tree
(`scripts/gates.py`).

## Privacy

Local-first. No facial recognition or identity profiling, and none is planned. The API binds
loopback by default and refuses a non-loopback bind without authentication. Model files are
downloaded only by an explicit command, from a fixed list, over HTTPS, and are integrity-checked
on every load.
