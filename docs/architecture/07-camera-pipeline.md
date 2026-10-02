# 07 — Camera Pipeline Design

> Status: DESIGN. No code exists.

## 1. The problem

Four source kinds with incompatible temporal semantics must feed one pipeline:

| Source | Arrival | Backpressure answer | Failure mode |
|--------|---------|---------------------|--------------|
| Webcam (USB) | Pushed at device fps | Drop — old frames are worthless | Device unplugged, exclusively locked by another app |
| RTSP / IP camera | Pushed over a lossy network | Drop, plus reconnect | Network loss, auth failure, stream restart, codec change |
| Video file | Pulled as fast as decode allows | **Block** — losing frames breaks reproducibility | EOF, corrupt frame |
| Still image | One frame, once | N/A | Unreadable file |

A pipeline that treats these identically gets one of them wrong. The usual mistake is
designing for live and then letting file replay silently drop frames, which makes every
test and benchmark non-reproducible. So the `FrameSource` protocol exposes the semantics
explicitly and the capture worker respects them.

## 2. FrameSource protocol

```python
class FrameSource(Protocol):
    @property
    def spec(self) -> SourceSpec: ...
    @property
    def timing(self) -> SourceTiming: ...     # LIVE | PACED | ONE_SHOT
    def open(self) -> SourceInfo: ...         # raises SourceError
    def read(self) -> Frame | None: ...       # None = end of stream (not an error)
    def close(self) -> None: ...
    @property
    def is_open(self) -> bool: ...
```

`SourceTiming` is what drives the policy choice:

- `LIVE` (webcam, RTSP) — latest-only buffering; skipping is normal and counted.
- `PACED` (video file) — blocking handoff; nothing is skipped. Optionally rate-limited to
  real time for demonstration, or run flat-out for benchmarking.
- `ONE_SHOT` (image) — one frame, then `None`.

`read()` returning `None` means end-of-stream, which is a normal lifecycle event. Errors
raise `SourceError`. Conflating "stream ended" with "stream broke" is why file replay
otherwise triggers reconnect storms at EOF.

## 3. Capture worker

One thread per camera. Responsibilities, in order: own the source, stamp frames, publish,
watch for stalls, reconnect. It does nothing else — no preprocessing, no detection, no
annotation.

```
loop:
    if not source.is_open:
        attempt_open()                  # backoff schedule, §5
        continue
    try:
        frame = source.read()           # blocking
    except SourceError:
        record_error(); transition(RECONNECTING); continue

    if frame is None:                   # end of stream
        if spec.loop and timing is PACED:  reopen(); continue
        transition(OFFLINE, "end of stream"); break

    stamp(frame)                        # epoch, index, monotonic + wall (§4)
    preroll.append(frame)               # §6
    publish(frame)                      # LATEST_ONLY or BLOCK per timing
    health.record_frame(frame)
```

Why one thread per camera rather than a shared polling loop: `VideoCapture.read()` blocks.
A shared loop means one unresponsive camera stalls every other camera — the dominant
failure mode of naive multi-camera designs. Thread cost is ~8 MB of stack plus the decode
buffers, and the GIL is released inside the decode, so this scales to the handful of
cameras this hardware can analyze anyway.

## 4. Clock discipline

The most important detail in this document, and the one most often got wrong.

Every frame is stamped **at capture**, in the capture thread, immediately after `read()`
returns:

```python
t_mono = clock.monotonic_ns()      # all arithmetic, ordering, decay
t_wall = clock.wall_utc()          # display, persistence, schedules
```

Rules:

1. **No stage downstream of capture reads a clock for decision purposes.** The temporal
   engine decays by the difference between frame timestamps, not by elapsed processing
   time. A GC pause or a slow disk write must not look like evidence decaying.
2. **Monotonic for arithmetic, wall for display.** Wall clocks jump: NTP corrections, DST,
   and laptop sleep/resume. A half-life computed across a wall-clock jump produces absurd
   confidence. Monotonic clocks do not jump but are meaningless to a human, so both are
   carried.
3. **File replay stamps from media PTS.** `ReplayClock` derives `t_monotonic_ns` from the
   frame's presentation timestamp, so a 30 fps file replayed at 300 fps still produces
   observations 33 ms apart in the temporal engine's view. **This is what makes replay
   results identical to live results**, which makes benchmarking and regression testing
   possible at all.
4. **Latency is measured, not assumed.** `t_processed - t_monotonic_ns` is recorded per
   stage, giving true end-to-end latency from photon to incident rather than inference time
   alone, which is the number people usually quote and which is usually a small fraction of
   the total.

A grep gate enforces rule 1 (01 §12).

## 5. Reconnection and health

### State machine

```
  DISABLED ---(enable)---> INITIALIZING
                                |
                  open ok       |      open fails
              +-----------------+------------------+
              v                                    v
           ONLINE <--- recovered ---          RECONNECTING
              |  \                                 |  \
              |   \ fps < threshold                |   \ attempts > max_attempts
              |    +------> DEGRADED               |    +------> FAILED
              |                |                   |                 |
              | stall / error  | persists          | (manual retry) -+
              +----------------+-------------------+
                                |
                         end of stream
                                v
                            OFFLINE
```

| State | Meaning | Pipeline behaviour |
|-------|---------|--------------------|
| `DISABLED` | Operator turned it off | No thread |
| `INITIALIZING` | Opening | No frames yet; UI shows a spinner, not an error |
| `ONLINE` | Healthy | Normal analysis |
| `DEGRADED` | Delivering, but below `health.min_fps_ratio` of expected, or decode errors above threshold | Analysis continues; **verification rejects candidates** (06 §6 validator 1) |
| `ANALYSIS_SUSPENDED` | Healthy stream, but the degradation ladder demoted it | Video streams to the console; no inference. Explicitly labelled in the UI |
| `RECONNECTING` | Lost, retrying | No analysis; tracker reset pending |
| `OFFLINE` | Ended cleanly (file EOF, or operator stop) | Terminal until restarted |
| `FAILED` | Retries exhausted | Terminal until manual retry; alerts once |

`DEGRADED` suppressing incidents rather than merely warning is deliberate: temporal
reasoning on a stream with irregular frame spacing is unsound, and an incident derived from
it would carry a confidence number it has not earned.

### Backoff

`min(base_ms * 2**attempt, max_ms)` with ±20% jitter — `ingest.reconnect_base_ms` default
500, `ingest.reconnect_max_ms` default 30000, `ingest.max_reconnect_attempts` default 0
(unlimited for live sources). Jitter matters with several cameras behind one NVR: without
it, a network blip makes every camera retry in lockstep and hammer the device.

### What a reconnect invalidates

On every successful reopen, `stream_epoch += 1`, and:

| State | Action | Why |
|-------|--------|-----|
| Tracker | `reset()`, `tracker_epoch += 1` | Track 7 before the gap is not track 7 after it. Reusing identities fabricates trajectories, which fabricates velocities, which fabricates incidents |
| Temporal accumulators for `track:` subjects | Discarded | Their subject no longer exists |
| Temporal accumulators for `zone:`/`region:`/`camera:` subjects | **Decayed across the gap, not discarded** | A fire does not stop burning because the network blipped. Decay across the gap is the honest answer |
| Pre-roll buffer | Retained | Pre-gap footage is still evidence, and is marked with a gap annotation in the manifest |
| Open incidents | Left open, annotated `stream_interrupted` | An operator must see that evidence has a hole, rather than the incident silently resolving |
| Module state | Reset via the epoch check (06 §4 rule 4) | |

The split in rows 2 and 3 is the subtle one, and it is the kind of detail that decides
whether a system is trustworthy across real-world network conditions.

### Stall detection

A stall is worse than a disconnect because the socket stays open and the code looks healthy.
A watchdog thread checks every camera every `ingest.watchdog_interval_ms` (default 1000): if
no frame has arrived in `max(3 x expected_frame_interval_ms, ingest.stall_timeout_ms)`
(default floor 5000), force-close the source and transition to `RECONNECTING`. The
multiplier adapts to the camera's own frame rate so a 2 fps camera is not declared stalled
for being slow.

## 6. Buffers

### LatestFrameSlot (live sources)

Capacity 1, overwrite-on-write, lock-protected, O(1).

```python
def put(self, frame: Frame) -> bool:       # returns False if it overwrote
def take(self) -> Frame | None:            # consumes
```

Overwrites increment `frames_skipped`. This is the correct behaviour for live video and it
must be *counted and displayed*, because the skip rate is how an operator knows the system
is not keeping up.

### BoundedQueue (inference -> analysis, analysis -> response)

`queue.Queue` with a declared `OverflowPolicy` (01 §9) and per-queue depth gauges. Depth is
the leading indicator of overload — it rises before FPS falls — which is why the
degradation ladder watches it.

### PreRollBuffer

A per-camera deque of **JPEG-encoded** frames, not raw arrays (open question Q3, now
settled by arithmetic): a 1080p BGR frame is 1920x1080x3 = 6.2 MB raw; five seconds at
25 fps across three cameras is 2.3 GB. The same window of quality-85 JPEGs is roughly
120 MB. Encoding costs ~2–4 ms per frame on CPU in a thread that is otherwise waiting on
I/O.

Bounded by both `evidence.preroll_seconds` and a hard `evidence.preroll_max_mb` per camera;
the byte cap wins. Encoding is done at `evidence.preroll_fps` (default 5), independent of
capture rate — pre-roll exists to show what led up to an incident, and 5 fps does that
while cutting the cost fivefold.

When an incident confirms, the response worker snapshots the deque (cheap: a tuple of
references), then muxes to MP4 off the hot path.

## 7. Windows-specific handling

These are the details that otherwise consume a day each.

### Webcam

- Default to `CAP_MSMF`; fall back to `CAP_DSHOW` if open fails or the first read times
  out. Some devices work on only one backend, and which one is not predictable.
- Request `MJPG` FOURCC before setting resolution. Many USB cameras expose high frame rates
  only in MJPG; on YUY2 they silently cap at 5–10 fps at 1080p.
- Set resolution **before** the first read and verify what was actually granted — cameras
  negotiate and may ignore the request. The granted values go in `SourceInfo` and the log;
  assuming the requested resolution is how downstream geometry quietly breaks.
- Identify cameras by name where possible, with index as fallback, because device indices
  are not stable across reboots or USB port changes. An index silently pointing at a
  different camera is a nasty failure.

### RTSP

- Force TCP. UDP over Wi-Fi produces torn frames and decode errors that look like camera
  faults. Via FFmpeg options set **before** the capture is constructed:
  `rtsp_transport;tcp|stimeout;5000000`.
- `stimeout` (microseconds) is essential — without a socket timeout, a dead stream blocks
  `read()` indefinitely and the watchdog is the only thing that notices.
- Try `CAP_PROP_BUFFERSIZE = 1`; FFmpeg often ignores it, which is precisely why the
  latest-only slot exists rather than being a nice-to-have.
- Credentials are injected into the URL at connection time from the secret store and the
  constructed URL is **never** logged or stored (01 §10, 05 §4). The log records
  `rtsp://host:554/<path>` with no userinfo.
- The env-var mechanism is process-global in OpenCV, so it is set once at startup from
  config, before any capture opens, and recorded in the log so a per-camera override
  attempt fails loudly rather than mysteriously.

### General

- `multiprocessing` requires `spawn`; the design avoids it entirely (01 §4).
- Paths via `pathlib.Path` throughout. Long-path and UNC handling comes free; string
  concatenation does not.
- Video writing uses `mp4v` or `avc1` depending on what the installed OpenCV build
  supports, probed once at startup by `vigil doctor` and recorded — not assumed.

## 8. Multi-camera scheduling

Capture is independent per camera. Inference is a shared, scheduled resource (04 §5), which
is the only point of contention.

```
cam-a (prio 9, 10 fps target) --> slot --+
cam-b (prio 5,  5 fps target) --> slot --+--> token-bucket scheduler --> batch <= 4
cam-c (prio 1,  2 fps target) --> slot --+          |
                                                     global ceiling: pipeline.global_inference_fps
```

Fairness: sort by `(priority, time_since_last_inference)`. The second term guarantees a
low-priority camera is eventually served rather than starved — a pure priority sort would
mean cam-c never runs while cam-a has frames available.

**Admission at startup.** `pipeline/graph.py` sums configured `target_inference_fps` across
enabled cameras and compares against `pipeline.global_inference_fps`. If oversubscribed, it
logs a WARNING naming the cameras and the computed effective rates, so the operator learns
at startup that cam-c will be analyzed at 2 fps rather than discovering it from missing
incidents weeks later.

## 9. Observability per camera

Published every `observability.sample_interval_ms` (default 1000), per camera:

| Metric | Why it is here |
|--------|----------------|
| `measured_fps` | Compared against expected to drive `DEGRADED` |
| `frames_received`, `frames_skipped`, `frames_dropped` | Skipped is design, dropped is failure (05 §4) |
| `decode_errors` | Rising count means network or codec trouble before fps visibly falls |
| `capture_to_infer_latency_ms` | Queue wait; the first thing to rise under load |
| `end_to_end_latency_ms` | Capture to incident. The number that matters and the one nobody measures |
| `reconnect_count`, `stream_epoch` | Stability over time |
| `preroll_bytes` | Proof the memory cap is holding |
| `state`, `state_duration_ms` | Drives the UI and the health timeline |

All of it feeds the Cameras and System views ([08-ui-architecture.md](08-ui-architecture.md))
and `SystemMetric` persistence (05 §4).

## 10. Failure matrix

| Failure | Detection | Response | Operator sees |
|---------|-----------|----------|---------------|
| USB camera unplugged | `read()` fails | `RECONNECTING`, backoff | Camera tile greyed, "reconnecting, attempt 3" |
| Camera locked by another app | `open()` fails with a specific error | `FAILED` after max attempts | "Device in use by another application" |
| RTSP auth failure | `open()` fails | `FAILED` immediately, **no retry** | "Authentication failed — check VIGIL_CAM_<ID>_PASSWORD". Retrying bad credentials can lock an account |
| Network blip | `read()` timeout | `RECONNECTING`, epoch bump on recovery | Brief "reconnecting"; open incidents annotated with a gap |
| Stream stalls, socket open | Watchdog | Force close, `RECONNECTING` | "Stream stalled, reconnecting" |
| Camera resolution changes mid-stream | Frame dimensions differ from `SourceInfo` | Epoch bump, tracker reset, zones recomputed from normalized coords | "Stream format changed" |
| Corrupt frames | Decode error counter | `DEGRADED` above threshold; incidents suppressed | "Degraded: 14 decode errors/min" |
| Disk full while writing evidence | Write fails | Incident still persisted, marked `evidence_partial`; alerts at CRITICAL | "Evidence capture failing: disk full" |
| Clock jump (NTP, resume) | Monotonic and wall drift diverge beyond threshold | Logged; temporal logic unaffected (monotonic); wall timestamps annotated | "System clock adjusted" in the system log |

The last row is the payoff for carrying two timestamps on every frame.

## 11. What this design does not do yet

To be recorded in `docs/LIMITATIONS.md` as each phase lands:

| Limitation | Consequence | Phase |
|------------|-------------|-------|
| No hardware-accelerated decode (NVDEC) | CPU decode limits total camera count before inference does | Evaluate at P6; the `FrameSource` protocol already allows a backend swap |
| No ONVIF discovery | Cameras are configured by URL in a file | Not planned; a convenience, not a capability |
| No PTZ control | View is fixed | Not planned |
| No audio | Visual only | Not planned |
| No cross-camera track identity | Each camera's tracks are independent | Past P6 (01 §2) |
| No recording of continuous video | Only incident-adjacent clips are kept | Deliberate: continuous recording is an NVR's job and would dominate disk use |
