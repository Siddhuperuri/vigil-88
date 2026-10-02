# VIGIL-88

A real-time computer-vision incident detection and situational-awareness platform, built
from first principles. Its defining idea is that it reasons **across time**: a frame is
evidence, not a verdict, and incidents come from accumulated, verified evidence rather than
per-frame classification.

## Honest status

**P0 (foundation) is implemented. Nothing is detected yet.**

| Works today | Does not exist yet |
|-------------|--------------------|
| Layered, validated configuration; structured logging with redaction | Object detection (only a null detector) |
| Frozen domain models for the whole data model | Tracking, scene analysis, events, incidents |
| Capability system (availability is computed, never claimed) | Evidence, alerts, persistence |
| Webcam capture, frame identity and timestamps, bounded buffering | RTSP, video files |
| Camera health state machine, reconnection, stall detection | HTTP API and web console |
| Headless application lifecycle, clean shutdown | Any performance claim |

See [docs/LIMITATIONS.md](docs/LIMITATIONS.md) for the full inventory. "Real-time" is not
claimed anywhere until `vigil bench` has measured it.

## Quick start

```powershell
uv sync --group dev --extra capture
uv run vigil doctor                      # environment, config, capabilities
uv run vigil run --selftest              # end-to-end check, no hardware
uv run vigil run --source webcam:0       # your webcam, through the null detector
powershell -File scripts/check.ps1       # the full quality gate
```

Operating notes: [docs/OPERATIONS.md](docs/OPERATIONS.md). Configuration reference:
[docs/CONFIG.md](docs/CONFIG.md) (generated).

## Design

The architecture is specified in [docs/architecture/](docs/architecture/README.md): ten
documents covering the engines, data model, event model, camera pipeline, UI, testing and
roadmap. Deviations found while implementing are recorded in
[docs/adr/0011](docs/adr/0011-p0-implementation-deviations.md).

Principles enforced by machine, not convention: layered imports (`import-linter`), a pure
domain layer, no reads of real time outside one clock module, no swallowed exceptions, no
credentials or machine paths in the tree (`scripts/gates.py`).

## Privacy

Local-first. No facial recognition or identity profiling, and none is planned. The API
(when it exists) binds loopback by default and refuses a non-loopback bind without
authentication.
