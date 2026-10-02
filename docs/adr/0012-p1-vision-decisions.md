# 0012 — P1 vision decisions and what real hardware taught us

- **Status:** accepted
- **Date:** 2026-10-02
- **Phase:** P1

Continues [0011](0011-p0-implementation-deviations.md). The design set remains the source of
truth; this records where P1 chose, deviated, or learned something the design could not know.

## A. Decisions

### A1. YOLOX over ONNX Runtime; no Ultralytics, no PyTorch

`03` planned Ultralytics for development velocity with ONNX Runtime as the licence-clean path,
and made "ONNX equals Ultralytics within tolerance" a P1 exit criterion. We built **only** the
ONNX path.

*Why.* Ultralytics is AGPL-3.0 and the network clause is triggered by exactly what this system
does (serving a UI). It also pulls in PyTorch (about 2.5 GB) for a second backend that adds no
capability the ONNX path lacks. YOLOX is Apache-2.0 and ships ready-made ONNX files, so the whole
GPU stack is licence-clean and PyTorch-free. The `Detector` protocol still isolates every backend,
so an Ultralytics backend remains a drop-in if ever wanted. The cross-backend equivalence criterion
is replaced by CPU-versus-CUDA parity (a test on the real GPU) and exact recovery of planted
boxes through decode, NMS and un-letterboxing.

### A2. ONNX Runtime pinned to 1.26.0

The newest ONNX Runtime GPU builds need CUDA 13, whose pip cuBLAS wheel has no Windows build.
1.26.0 is the newest release on CUDA 12, whose NVIDIA wheels do ship for Windows, so the GPU
stack installs from pip with no CUDA toolkit. CPU and GPU builds provide the same module and
cannot coexist, so they are conflicting extras (`onnx-cpu`, `onnx-gpu`).

### A3. Resolution is a model choice

The released YOLOX ONNX files have a static `1x3xHxW` input and batch of 1. So `input_size_px`
defaults to "the model's own size" and a different value is a startup error rather than a silent
resize; the degradation ladder's resolution tiers (`01 §9`) cannot apply to these models; and a
"batch" of N frames is N sequential runs. Cross-camera batching (`D-004`) therefore buys nothing
yet. A dynamic-batch export is the way to recover it.

### A4. Strict, explicit model handling

Models are never downloaded implicitly. `vigil models fetch` uses a fixed registry of HTTPS URLs,
checks the exact byte count, writes atomically and records the SHA-256 in `MANIFEST.json`. Every
load verifies size and hash; an unlisted, missing, truncated or changed file is a hard error with
the fix named. Upstream publishes no checksums, so the first fetch is trust-on-first-use; that is
stated, not hidden. `weights` is a bare file name, never a path.

### A5. Server-side overlays, encoded once

`08 §4` is implemented as designed. Boxes are drawn where the pixels and the detections made on
them are both held, so the overlay cannot drift (checked on pixels: the changed pixels equal the
planted box outline). Streams are reference-counted and encoded once per distinct overlay however
many viewers there are; with no viewer nothing is encoded.

### A6. The API never runs a model

An architecture test fails if any `vigil.api` module imports the vision engine or an inference
runtime, and another fails if anything outside `vigil.api` imports the web framework.

### A7. The default model is YOLOX-S

Screening runs of nano, tiny and S on the development GPU showed all three exceed the 30 fps
target; S is the most accurate of the three, so it is the default. Sustained 120 s results (see
[PERFORMANCE.md](../PERFORMANCE.md)) show S holds 30 fps with no skipped or dropped frames. Its
capacity is about 58 to 60 fps, limited by one saturated CPU core of Python-side work rather than
by the GPU (utilisation peaked at 75%). At saturation the GPU reported `sw_thermal_slowdown` for
25 of 120 s with little change in clocks. The CPU fallback manages about 13 fps. A laptop GPU at
a low duty cycle is about five times slower per inference (64 ms against 13 ms) because its
clocks idle down; this is why the 5 fps run is published beside the 30 fps run.

## B. Defects found by running on real hardware

None of these were visible to unit tests with fakes. Each now has a regression test.

| Finding | Cause | Fix |
|---------|-------|-----|
| "CUDA" detector was silently running on CPU (62 ms, GPU idle at 420 MHz) | ONNX Runtime's Python layer retries a failed run on CPU and still reports success; I had checked the active providers *before* the first run | `disable_fallback()`; warm-up failure is treated as a CUDA failure; policy is `vision.allow_cpu_fallback` |
| cuDNN could not find `cudnn_engines_tensor_ir64_9.dll` | cuDNN loads sub-libraries by bare name; preloading is not enough | The NVIDIA wheel `bin` directories are put on the DLL search path before the first CUDA session |
| All latencies were multiples of ~15.6 ms | `time.monotonic_ns()` ticks at 15.6 ms on Windows | `SystemClock` uses `perf_counter_ns` |
| A 30 fps camera with a 30 fps target got 21 fps (capacity was ~46) | The inference loop slept a fixed 20 ms after an empty poll; service time plus sleep exceeded the frame period so every other frame was overwritten | The worker sleeps until a frame arrives or the next token is due; token buckets gain a burst allowance |
| Every per-camera API route returned 500 | FastAPI could not resolve a type alias defined inside `create_app` under `from __future__ import annotations` | Explicit validation call |
| A viewer that left a stalled camera's stream was never noticed | The sync generator never yielded, so the server saw no disconnect; the viewer slot leaked and shutdown waited on open streams | Async generator polling `is_disconnected()`; hub closed before the server stops; graceful-shutdown timeout |
| A real-time read slipped into the stream route | Per-viewer rate limiting used `time.monotonic()` | Caught by the repository gate; uses the injected clock |
| Pydantic's own error text echoed a rejected RTSP password (P0) | `ValidationError.__str__` includes `input_value` | `hide_input_in_errors` on every config model |

## C. Deviations from the documents

- `pipeline.global_inference_fps` keeps its design default of 20. It silently caps a faster
  camera, which surprised two of my own tests; it is documented in LIMITATIONS and the benchmark
  tooling sets it explicitly.
- `SystemMetric` gained process CPU, SM clock, power and throttle reasons, because a laptop-GPU
  benchmark is uninterpretable without them.
- A `vigil bench` package, `vigil models` commands and an `api` extra were added to the layering
  contract (`cli` > `api | bench` > `pipeline`).
- **Not built**, to keep P1 to what was asked: the degradation ladder, a supervisor with restart,
  FP16 (refused explicitly), and the React console (a provisional viewer exists).
