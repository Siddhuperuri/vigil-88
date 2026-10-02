# 0011 — P0 implementation deviations from the design set

- **Status:** accepted
- **Date:** 2026-10-02
- **Phase:** P0

The design set is the source of truth. Implementing it exposed two genuine
inconsistencies and several places where a smaller choice was clearly better. None changes
an architectural decision (D-001 to D-010); all are recorded here rather than silently
absorbed.

## A. Contradictions in the design, and how they were resolved

### A1. `core/protocols` must name domain types, but `core` is below `domain`

`01 §5` places `core/` at L0 (stdlib only) and `domain/` at L2, and requires protocols in
`core/protocols/`. But `Detector.detect_batch` takes `PreparedFrame` and returns
`DetectionSet`, which are domain types. As written, either protocols import upward or
`domain` cannot be below `core`.

**Resolution.** Protocol modules reference domain types **only in annotations, under
`TYPE_CHECKING`**, and `import-linter` runs with `exclude_type_checking_imports = true`.
There is no runtime import from `core` to `domain`; the layering contract and a test
(`test_core_and_domain_import_nothing_from_higher_layers`, plus a cycle detector) verify it.
Types that are genuinely needed at runtime inside `core` (`Capability`, `PixelBuffer`) live
in `core`. *Rejected:* moving protocols into a new layer above `domain` (changes the
documented directory structure for no behavioural gain).

### A2. `observability/probes.py` and `health.py` handle domain types

`02` puts them in L1 `observability/`, but `SystemMetric` and `CameraHealth` are L2 domain
types; L1 importing them is an upward import.

**Resolution.** `observability/probes.py` returns a plain `SystemReading`; the composition
into `SystemMetric` is `pipeline/sampler.py`, and health aggregation is
`pipeline/health.py` (L5). `observability` stays free of domain imports.

### A3. `FrameSource.read()` returns a `Frame`, but a `Frame` needs capture-time stamps

`07 §2` says `read() -> Frame | None`, while `07 §3` has the worker `stamp(frame)`
afterwards. A `Frame` is frozen and carries epoch, index and timestamps that only the
capture worker can assign, immediately after `read()` returns (`07 §4`).

**Resolution.** Sources return a `RawFrame` (pixels, optional PTS, keyframe flag); the
capture worker builds the immutable `Frame`. This makes the clock rule structural.

## B. Dependency choices (rule 6 of `03`: prefer the stdlib)

| Design said | Implemented | Reason |
|-------------|-------------|--------|
| `pydantic-settings` | not used | The layered loader (yaml, env, `$include`, interpolation) needs custom sources anyway; plain pydantic gives the same validation with one fewer dependency. `.env` is parsed into a dict and never pushed into `os.environ` |
| `python-ulid` | own 40-line monotonic ULID in `core/ids.py` | Keeps `core` stdlib-only and makes ids reproducible from an injected clock and RNG |
| `keyring` for secrets | not implemented | Environment variables and `.env` only; listed in `LIMITATIONS.md` |
| `opencv-contrib-python-headless` | `opencv-python-headless` | `contrib` is not needed until calibration/trackers arrive |
| `opencv` in the `vision` extra | separate `capture` extra | A webcam must not require torch. The future `vision` extra will include `capture` |
| `sqlalchemy`, `alembic`, `fastapi`, `uvicorn`, `pynvml` | not installed | Their consumers (persistence, API, GPU probing) are out of P0 scope |

## C. Smaller additions and omissions

- **Config keys carry units** (`input_size_px`, `vram_budget_mb`), following the README rule;
  `05 §7` showed some without. The generated `docs/CONFIG.md` is authoritative.
- **`ingest.max_initial_open_attempts`** (default 3) is new. With unlimited reconnects
  (`07 §5`) a camera that never existed would sit in RECONNECTING forever; opens that fail
  *before the camera ever came online* now end in FAILED, while reconnects after a working
  stream stay unlimited.
- **`TransientSourceError`** and `ingest.max_consecutive_read_failures`: one bad read is a
  decode error counted toward DEGRADED, not a reconnect; a run of them escalates.
- **Stall handling is detect-and-report, not force-close.** `07 §5` says a watchdog
  force-closes a stalled source. Releasing an OpenCV capture from another thread while
  `read()` blocks is not thread-safe. The watchdog marks the camera DEGRADED ("stalled"),
  which suppresses incidents, and recovery is cooperative. Revisit when a source with a
  read timeout (RTSP via FFmpeg `stimeout`) exists.
- **Omitted from P0 by instruction:** persistence, the API/UI, video-file and image
  sources, `MockDetector`, the synthetic *scene* generator (a deterministic test-pattern
  source exists instead), `result.py`, a generic `BoundedQueue`, `PreRollBuffer`, the
  supervisor/restart policy, and the token-bucket scheduler.
- **`SystemMetric`** pipeline/latency fields are `float | None`, not `float`, honouring
  `05 §1` rule 5 (no sentinel zeros).
