# Limitations

The honest gap between the architecture and the implementation. Updated at every phase.
Nothing here is hidden behind a control that appears to work (README rule 1).

**Current phase: P0 (foundation).** The system captures frames and runs them through a
detector that detects nothing. It does not detect, track or reason about anything yet.

## What does not exist

| Area | State | Planned |
|------|-------|---------|
| Object detection | Only the **null detector**: valid, empty output. `DETECTION` capability is **not** granted | P1 |
| ONNX / Ultralytics / GPU inference | Not implemented. Selecting one fails startup with a `CapabilityError` | P1 |
| Tracking, scene analysis, zones | Interfaces and domain types only; `TRACKING`, `ZONES` etc. unavailable | P2 |
| Temporal engine, event modules, verification, severity | Config schema and domain types only. **No module exists**; `vigil doctor` says so | P3 |
| Incidents, evidence, persistence, retention | Domain types only. No database, no incident is ever created | P4 |
| Alert channels | Protocol only. No channel exists | after P4 |
| HTTP API, WebSocket, web console, live stream | Not built. The core is headless | P1+ |
| RTSP / IP cameras | Config validates (and rejects inline credentials). A camera of this kind is reported **FAILED: not implemented** | later |
| Video-file replay, image input | Not built. FAILED: not implemented | later |
| Windows Credential Manager (`keyring`) | Not implemented. Secrets come from environment variables or `.env` | when needed |
| GPU metrics (utilisation, VRAM, temperature) | Not probed; reported as `None`, never `0` | P1 |
| Ground-plane calibration, m/s velocity | Not built | P5 |

## Known behaviours and constraints

- **A blocked webcam read cannot be cancelled.** OpenCV's `read()` has no timeout and
  closing a capture from another thread is not safe. The watchdog detects a stall and marks
  the camera DEGRADED, but a read that never returns would leave its worker thread blocked.
  Shutdown still completes: it waits `pipeline.shutdown_timeout_ms`, then reports the
  straggler by name and returns. (ADR 0011.)
- **Webcam open can be slow on Windows** (MSMF may take several seconds) and cannot be given
  a timeout.
- **No automatic worker restart.** A crashed worker is logged, counted and makes the
  application report FAILED health; there is no supervisor yet.
- **Capture→inference latency is only reported for live sources.** Replay cameras run on a
  PTS-driven clock that is not comparable to the application clock.
- **The synthetic source is a test pattern**, not a scene simulator. It produces no
  detections and exists for selftest and determinism tests.
- **Hardware coverage:** the real-webcam test is opt-in (`pytest --webcam`) because it lights
  the camera. Behaviour on other cameras/drivers is unverified.
- **Real-world false-positive rate is unknowable from P0.** It needs real footage and, from
  P4, the measured dismissal rate.
- **No performance claim is made.** `docs/PERFORMANCE.md` does not exist until `vigil bench`
  produces it (P1).
