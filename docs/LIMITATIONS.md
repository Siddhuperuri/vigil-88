# Limitations

The honest gap between the architecture and the implementation. Updated at every phase.
Nothing here is hidden behind a control that appears to work (README rule 1).

**Current phase: P1 (vision and live camera).** The system captures frames, detects objects with
a real model on the GPU (or CPU), annotates them and serves a live stream with metrics. It does
not track, reason over time, or raise incidents yet.

## What does not exist

| Area | State | Planned |
|------|-------|---------|
| Tracking, scene analysis, zones | Interfaces and domain types only; `TRACKING`, `ZONES` etc. stay unavailable | P2 |
| Temporal engine, event modules, verification, severity | Config schema and domain types only. **No module exists**; `vigil doctor` says so | P3 |
| Incidents, evidence, persistence, retention | Domain types only. No database; no incident is ever created | P4 |
| Alert channels | Protocol only | after P4 |
| Web operations console | Not built. A **provisional viewer** at `/` shows the live streams and metrics; it is not the console | later |
| RTSP / IP cameras | Config validates (and rejects inline credentials). Such a camera is reported **FAILED: not implemented** | later |
| Video-file replay, image input | Not built | later |
| Ultralytics backend | Deliberately not built (AGPL, drags in PyTorch); see ADR 0012 | only if needed |
| Degradation ladder, supervisor/restart | Not built. The scheduler bounds load, but nothing lowers resolution or demotes cameras under overload, and a crashed worker is reported, not restarted | later |
| Windows Credential Manager (`keyring`) | Not implemented; secrets come from environment variables or `.env` | when needed |
| Ground-plane calibration, m/s velocity | Not built | P5 |

## Detection: what it is and what it is not

- **One model family.** YOLOX (Apache-2.0), COCO-trained, via ONNX Runtime. Three sizes are in
  the registry: `yolox_nano` and `yolox_tiny` at 416 px, `yolox_s` at 640 px.
- **Detection quality is not evaluated.** We consume published weights and have no labelled
  dataset, so there is no mAP, precision or recall figure anywhere. The only accuracy evidence is
  qualitative: the annotation was checked against one real webcam scene and a person was boxed
  correctly. Do not read the benchmark numbers as accuracy numbers.
- **Only some classes are mapped.** COCO has 80 classes; the system maps the ones it has a
  canonical meaning for (person, bicycle, car, motorcycle, bus, truck, bags, animals). Every
  other detection (chairs, books, cups ...) is dropped and counted per label in
  `vigil_unmapped_labels_total`. Cluttered scenes produce many of these; that is expected.
- **Fixed input size and static batch of 1.** These exports cannot change resolution at run time
  or process a batch. "Configurable resolution" therefore means *choosing a model* (`yolox_s`
  at 640 px or `yolox_nano`/`yolox_tiny` at 416 px). `vision.input_size_px` must equal the
  model's size or startup fails. Frames from several cameras are inferred one after another,
  so cross-camera batching gains nothing yet.
- **No FP16.** `vision.precision: fp16` is refused rather than silently ignored. On the CUDA
  provider the Ampere GPU uses TF32 for convolutions and matrix multiplies (reported as
  `fp32+tf32` in the model descriptor); this is a numerical-precision trade the provider makes.
- **Trust on first use.** Upstream publishes no checksums for the YOLOX weights, so the SHA-256
  recorded on first fetch is only as trustworthy as that first download. It is verified on every
  load afterwards.

## Hardware and performance

- **No result here is a promise.** All performance figures live in
  [PERFORMANCE.md](PERFORMANCE.md), are generated from stored measurements, and describe one
  machine: an RTX 3050 6 GB *Laptop* GPU on AC power, with a normal desktop's other applications
  running. Different power settings, battery operation, thermals or background load will change them.
- **Latency depends on how busy the GPU is.** A laptop GPU lowers its clocks when it is not
  working hard, so a sparse trickle of inferences (a few per second) runs at a fraction of the
  clock a saturated GPU holds. The same model is measurably slower per frame at low duty
  cycle. PERFORMANCE.md shows this rather than hiding it.
- **Synthetic benchmark sources** are test patterns with nothing for a detector to find, so their
  post-processing cost is that of an empty scene. The webcam runs cover real-scene cost.
- **CUDA 12 only.** ONNX Runtime 1.26 is pinned because later releases need CUDA 13, whose pip
  cuBLAS package has no Windows build. Only Windows and the one GPU above are verified.
- **Camera health trusts the driver's claimed frame rate.** The development webcam reports 30 fps
  but delivers about 15 in the indoor light of the benchmark, so the health tracker marks it
  DEGRADED for the whole run although frames arrive steadily. The webcam benchmark's
  health-issue count reflects this; the tracker should compare against the measured rate.
- **A blocked webcam read cannot be cancelled.** OpenCV's `read()` has no timeout and closing a
  capture from another thread is not safe. The watchdog detects a stall and marks the camera
  DEGRADED. Shutdown still completes: it waits `pipeline.shutdown_timeout_ms`, reports the
  straggler by name and returns. (ADR 0011.)
- **Webcam specifics.** On the development machine OpenCV 5.0 cannot open the camera through
  MSMF (`isOpened()` is false immediately), so the automatic fallback uses DirectShow, which opens
  in about 0.5 s and delivers its first frame about 0.8 s later. The driver reports no frame rate,
  so the stall threshold falls back to `ingest.stall_timeout_ms`.
- **The default global inference ceiling is 20 fps** (`pipeline.global_inference_fps`). It bounds
  total GPU load and silently caps a faster camera, by design; raise it for higher rates. The
  benchmark tooling sets it explicitly.

## API and security

- **Authentication and browsers.** With `api.auth.enabled`, every API route requires a bearer
  token (constant-time comparison; a non-loopback bind refuses to start without it). A browser
  `<img>` cannot send headers and tokens must never go in URLs, so with auth enabled the MJPEG
  stream works for programmatic clients but not for the bare `<img>` in the provisional viewer.
  The session flow that fixes this belongs to the full console.
- **MJPEG only**, at most `api.stream_fps`. WebRTC sits behind the `VideoTransport` protocol if
  measurement ever demands it.
- **One process, one operator.** No users, roles or audit trail.

## Other

- **No automatic worker restart.** A crashed worker is logged, counted and makes the application
  report FAILED health.
- **Capture-to-inference latency is only reported for live sources.** Replay cameras run on a
  PTS-driven clock that is not comparable to the application clock.
- **The synthetic source is a test pattern**, not a scene simulator.
- **Hardware coverage.** The real-webcam test is opt-in (`pytest --webcam`) because it lights the
  camera. Behaviour on other cameras and drivers is unverified.
- **Real-world false-positive rate is unknowable from here.** It needs real footage and, from P4,
  the measured dismissal rate.
