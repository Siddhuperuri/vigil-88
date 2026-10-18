# Operations

Runbook for the current phase (P5). It documents only what exists.

## Setup

```powershell
uv sync --group dev --extra onnx-gpu --extra api   # full workstation: GPU detector + live API
uv sync --group dev --extra onnx-cpu --extra api   # no NVIDIA GPU: CPU detector
uv sync --group dev --extra capture                # webcam capture only (null detector)
uv sync --extra reports                            # add PDF export (optional; JSON/CSV need nothing)
uv sync --group dev                                # logic and tests only; no OpenCV, no models
```

The GPU build pulls its CUDA 12 and cuDNN libraries from pip wheels (about 1.5 GB); no CUDA
toolkit install is needed. `onnx-cpu` and `onnx-gpu` cannot be installed together.

## Models

Weights are never downloaded implicitly and never committed. They live in `var/models/`
(gitignored), and every file must be listed in `var/models/MANIFEST.json` with its SHA-256.

```powershell
uv run vigil models list                      # what is known, present and verified
uv run vigil models fetch yolox_s             # HTTPS download, size check, hash recorded
uv run vigil models verify                    # re-check every file against the manifest
uv run vigil models register my.onnx --license MIT --source "my export"
```

| name | input | size | licence |
|------|-------|------|---------|
| `yolox_nano` | 416 px | 3.7 MB | Apache-2.0 |
| `yolox_tiny` | 416 px | 20.2 MB | Apache-2.0 |
| `yolox_s` | 640 px | 35.9 MB | Apache-2.0 |

A model that is missing, unlisted, truncated or changed after it was recorded is a hard startup
error that names the fix. There is no "run anyway".

## Running

```powershell
uv run vigil doctor                           # environment, config, GPU, models, detector, capabilities
uv run vigil doctor --probe-webcam 0          # open the camera, read 5 frames (lights the LED)
uv run vigil run --selftest                   # end-to-end check on a test pattern, no hardware
uv run vigil run --source webcam:0 --detector yolox_s --serve
```

`--serve` starts the API and prints the viewer URL (default `http://127.0.0.1:8088/`; override
with `--port`). `--detector NAME` selects a model; `--device auto|cuda|cpu` selects where it runs.
To make a choice permanent put it in `config/vigil.local.yaml`:

```yaml
vision:
  backend: onnxruntime
  weights: yolox_s
  device: auto
```

`vigil run` refuses to start with no enabled camera, and the committed example camera is
disabled: **nothing opens a camera you did not ask for.**

Exit codes: `0` ok, `1` runtime failure (including an unclean shutdown), `2` usage or
configuration error.

### Device choice and fallback

`device: auto` prefers CUDA and logs why when it does not get it. `cuda` asks for it explicitly.
When CUDA was wanted but cannot start or run, `vision.allow_cpu_fallback` decides:
`true` (default) continues on CPU **loudly** (an ERROR log, a `vigil_detector_fallbacks_total`
count, and the model descriptor shows `cpu (fell back from cuda)`); `false` fails instead.
A benchmark always uses `false`, so it can never measure the wrong device.

## The API

Served by `--serve`. Reads immutable snapshots; it never runs a model.

| route | purpose |
|-------|---------|
| `GET /` | provisional live viewer |
| `GET /api/v1/system/health` | state, health level and issues, detector, capabilities |
| `GET /api/v1/system/metrics` | FPS, latency by stage, CPU/GPU/VRAM, counters (GPU fields are `null` when unmeasured) |
| `GET /api/v1/system/info` | version, config hash, model |
| `GET /api/v1/cameras`, `/cameras/{id}` | camera health: state, fps, received/skipped/dropped |
| `GET /api/v1/cameras/{id}/detections` | the latest detections, in source-frame pixels |
| `GET /api/v1/cameras/{id}/snapshot.jpg?overlay=` | one annotated (or raw) frame |
| `GET /api/v1/cameras/{id}/stream.mjpeg?overlay=&fps=` | live annotated MJPEG |
| `GET /api/v1/cameras/{id}/modules` | per-module availability: ACTIVE, DEGRADED or UNAVAILABLE, with the reason and what would grant the missing capability |
| `GET /api/v1/cameras/{id}/zones/status` | configured zones and whether each is usable |
| `GET /api/v1/incidents?...` | the incident page: filter, sort, keyset paginate |
| `GET /api/v1/incidents/{id}` | one incident: explanation, timeline, evidence, permitted actions |
| `GET /api/v1/incidents/{id}/evidence` | the manifest, including items that are **missing and why** |
| `GET /api/v1/incidents/{id}/evidence/{eid}` | the file, with `X-Evidence-Integrity: ok\|mismatch` |
| `POST /api/v1/incidents/{id}/acknowledge\|dismiss\|escalate\|annotate` | operator actions; a forbidden transition is a 409 naming the rule |
| `DELETE /api/v1/incidents/{id}/annotate` | clear the note |
| `GET /api/v1/events/candidates`, `/candidates/reasons`, `/timeline` | what was *rejected*, and by which validator |
| `GET /api/v1/mutes`, `/mutes/history` · `POST /api/v1/mutes` · `DELETE /api/v1/mutes/{id}` | suppression with an expiry and a reason |
| `GET /api/v1/system/channels` | alert channels and whether each can actually deliver |
| `GET /api/v1/analytics/summary?days=&bucket=&...` | counts, trends and breakdowns over a window |
| `GET /api/v1/analytics/report?...&rows=` | the same summary plus incident rows and their timelines |
| `GET /api/v1/analytics/export?format=csv\|json&save=` | download, or write into `paths.exports_dir` |
| `GET /api/docs` | OpenAPI documentation |

`overlay` is `none` or a comma list of `boxes`, `labels`, `info`. The overlay is drawn on the
server from the detections made on that exact frame, so it cannot drift from the video.
Streams are encoded only while someone watches, once per distinct overlay however many viewers.

The API binds `127.0.0.1`. Binding anything else requires `api.auth.enabled: true` and a token
of at least 32 characters in `VIGIL_API_TOKEN`, or startup is refused.

## Incidents and evidence

A verified event becomes one durable incident, keyed so that repeated evidence about the same
situation **extends** it rather than creating another. Nothing is deleted implicitly.

Where things live, all under `paths.data_dir` unless the key beside it is set:

| path | holds | key |
|------|-------|-----|
| `db/vigil.sqlite3` | incidents, evidence rows, candidates, operator actions, mutes, metrics | `storage.database_file` |
| `evidence/<incident>/` | `snapshot.jpg`, `clip.mp4`, `manifest.json` | `paths.evidence_dir` |
| `exports/` | saved analytics exports | `paths.exports_dir` |

- **An incident's evidence is listed even when an item is absent**, with the reason. A missing
  clip leaves `evidence_status: partial`, never a zero-byte file and never a substitute frame.
- **Every evidence read is hashed and compared.** A mismatch is served with
  `X-Evidence-Integrity: mismatch` and labelled in the viewer rather than withheld silently.
- **Retention is time-based and per kind** — `retention.clip_days` (30), `snapshot_days` (90),
  `candidate_days` (7), `metric_days` (14), `incident_days` (365) — swept every
  `retention.sweep_interval_s`. It deletes files before rows and incidents last, so an incident
  never references evidence that has gone. **An open incident and an annotated one are never
  deleted automatically**, at any age.
- **`storage.enabled: false` is supported and visibly different.** Events are analysed and
  nothing is stored; `GET /api/v1/system/health` reports `persists_incidents: false` and the
  viewer says so instead of showing an empty list.

## Analytics and exports

The Analytics panel in the viewer, and the three routes above, aggregate **only what was
persisted**. Read them with two rules in mind:

- **"unavailable" means the records cannot answer**, not zero. Time-to-acknowledge with nothing
  acknowledged is unavailable; a camera with no recorded health history is `unknown`, not 100%
  online.
- **The dismissal rate is not a false-positive rate.** It is what operators dismissed, per module
  version. Each number carries its own definition; `GET /analytics/summary` returns them in
  `metric_definitions`.

```powershell
# CSV for a quarter, written into paths.exports_dir
curl "http://127.0.0.1:8088/api/v1/analytics/export?days=90&format=csv&save=true&name=q1-review"
```

Exports are JSON, CSV and — with `--extra reports` installed — PDF. Without that extra,
`format=pdf` answers **503 naming the install command** rather than silently returning something
else, and the viewer's PDF button is disabled carrying the same reason. A `name` that is not a
plain filename is replaced, not cleaned, and every write lands inside `paths.exports_dir`.
`GET /api/v1/analytics/formats` says which formats this install can actually produce.

## Calibrating a camera for metres

Optional. Without it everything works and velocity is reported in px/s, labelled px/s.

1. Pick something flat and measurable in view: a parking bay, four floor tiles, a doormat.
2. Measure the four corners with a tape, in metres, from any origin you like, as long as it lies
   in the floor plane.
3. Read the same four corners' pixel positions off
   `GET /api/v1/cameras/<id>/snapshot.jpg`.
4. Copy `config/calibration/example-floor.calib.yaml`, fill in your numbers and the frame size,
   and set `calibration_ref: <name>` on the camera.
5. `uv run vigil doctor` then reports `ground_plane` as available, with the file as its provider.

What you get is m/s **for things standing on that plane**, converted at each track's foot point
because metres-per-pixel varies across a perspective view. What you do not get is anything off
the plane — a staircase, a ramp, a thrown object — and no accuracy check: a four-point fit passes
exactly through whatever four points you give it. A broken or missing calibration costs the
capability, not the camera: `vigil doctor` names the reason and the pipeline keeps running.

## Benchmarking

```powershell
uv run vigil bench run --detector yolox_s --device cuda --name my-run --duration 120 --publish
uv run vigil bench report                     # rewrite docs/PERFORMANCE.md from stored results
```

A run shorter than 120 s is a *screening* run and is labelled as such. `--publish` copies the
result JSON to `docs/bench-results/`; `report` generates `docs/PERFORMANCE.md` from those files
only. Run benchmarks on AC power with other programs closed; each result records whether the
machine was plugged in and which commit produced it.

## Reading the status line

```
state=running health=ok [cli-webcam-0 online 15.0fps rx=300 skip=200 drop=0] inferred=100 infer_p50=15.8ms pipeline=5.0fps detections=100 gpu=33% vram=1388MB 52C
```

- `skip` is frames intentionally not analysed (the camera outpaces the inference target). Normal.
- `drop` is frames lost to a full queue. **Should stay 0.**
- `health`: `ok`, `degraded` (a camera reconnecting, stalled or failed), `failed` (a worker
  crashed, or no camera is usable).

## Camera states

`initializing`, `online`, `degraded` (stalled, low fps or decode errors), `reconnecting`,
`offline` (ended or stopped), `failed` (retries exhausted or not implemented), `disabled`.
A webcam that cannot be opened is retried `ingest.max_initial_open_attempts` times (default 3)
and then marked `failed`; after it has worked once, reconnects continue indefinitely.

## Logs

JSON lines in `var/logs/vigil.jsonl` (rotating), human-readable on the console. Credentials are
redacted from every record.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `model file ... not found` / `not listed in MANIFEST.json` | `vigil models fetch <name>` |
| `failed its integrity check` | The file changed after it was recorded. Re-fetch, or investigate how it changed |
| `CUDA unavailable, falling back to CPU` in the log | `vigil doctor` shows the providers and the reason. Install `--extra onnx-gpu`; on Windows the CUDA/cuDNN pip wheels must be present |
| `Could not locate cudnn_*.dll` | The NVIDIA wheel directories must be on the DLL search path; `vigil` does this before the first CUDA session. If you load ONNX Runtime yourself, call `vigil.vision.device.prepare_cuda_runtime()` first |
| Latency is 3-4x higher than the benchmark | A laptop GPU runs slower at a low duty cycle (clocks drop between sparse frames), and on battery. Check `gpu_sm_clock_mhz` in `/api/v1/system/metrics` |
| `fixed 416px input but vision.input_size_px is 640` | Remove `input_size_px` (it defaults to the model's size) or choose a model exported at that size |
| `the API could not start ... already in use` | Another process owns the port: `--port` |
| Webcam `failed`, "in use by another application" | Close other apps using the camera; check Windows camera privacy settings |
| `capture extra: not installed` | `uv sync --extra capture` (included in the ONNX extras) |
| Shutdown reports a straggler | A webcam read did not return in time (see LIMITATIONS.md). The process still exits |
