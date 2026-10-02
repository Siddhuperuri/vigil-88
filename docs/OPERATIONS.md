# Operations

Runbook for the current phase (P0). It documents only what exists.

## Setup

```powershell
uv sync --group dev --extra capture     # development install with webcam support
uv sync --group dev                     # without OpenCV/NumPy: logic and tests only
```

## Commands

| Command | Purpose |
|---------|---------|
| `uv run vigil doctor` | What is installed, what the configuration is, which capabilities exist. Exits 1 on a real problem |
| `uv run vigil doctor --probe-webcam 0` | Open webcam 0, read 5 frames, report the granted resolution and backend. **Lights the camera LED** |
| `uv run vigil doctor --dump-config docs/CONFIG.md` | Regenerate the configuration reference |
| `uv run vigil run --selftest` | Start the real application on the deterministic test pattern and check the result. No hardware |
| `uv run vigil run --source webcam:0` | Capture from a webcam through the null detector. Ctrl+C stops it cleanly |
| `uv run vigil run --source synthetic:300 --duration 10` | Same, on the test pattern, for 10 s |
| `powershell -File scripts/check.ps1` | The full quality gate |

`vigil run` refuses to start with no enabled camera, and the committed example camera is
disabled: **nothing opens a camera you did not ask for.**

Exit codes: `0` ok, `1` runtime failure (including an unclean shutdown), `2` usage or
configuration error.

## Configuration

Layers, last wins: defaults, `config/vigil.yaml`, `config/vigil.local.yaml` (gitignored),
`VIGIL_<SECTION>__<KEY>` environment variables (and `.env`), CLI flags. Unknown keys are
errors. Every key: [CONFIG.md](CONFIG.md).

```powershell
$env:VIGIL_VISION__MAX_BATCH_SIZE = "2"      # override one key
$env:VIGIL_CONFIG_DIR = "D:\vigil-config"    # use a different config directory
```

Secrets never go in YAML. Reference them (`${env:NAME}`) or put them in `.env`
(gitignored; see `.env.example`). An RTSP URL with inline credentials is rejected at load.

## Reading the status line

```
state=running health=ok [cli-webcam-0 online 29.8fps rx=300 skip=142 drop=0] inferred=158 detections=0
```

- `skip` is frames intentionally not analysed (a live camera outpaces inference). Normal.
- `drop` is frames lost to a full queue. **Should stay 0.**
- `health`: `ok`, `degraded` (a camera reconnecting, stalled or failed), `failed` (a worker
  crashed, or no camera is usable).

## Camera states

`initializing`, `online`, `degraded` (stalled, low fps or decode errors), `reconnecting`,
`offline` (ended or stopped), `failed` (retries exhausted or not implemented), `disabled`.
A webcam that cannot be opened is retried `ingest.max_initial_open_attempts` times (default
3) and then marked `failed`; after it has worked once, reconnects continue indefinitely.

## Logs

JSON lines in `var/logs/vigil.jsonl` (rotating), human-readable on the console. Credentials
are redacted from every record.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `capture extra: not installed` | `uv sync --extra capture` |
| Webcam `failed`, "in use by another application" | Close other apps using the camera; check Windows camera privacy settings |
| Webcam opens but reports a different resolution than configured | Expected: the camera granted what it supports. The log line `source opened` shows the granted values |
| `vision backend 'x' is not implemented` | Only `null` exists in P0; set `vision.backend: "null"` |
| Shutdown reports a straggler | A webcam read did not return in time (see LIMITATIONS.md). The process still exits |
