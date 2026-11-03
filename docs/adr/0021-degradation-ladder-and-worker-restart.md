# 0021: The degradation ladder and restarting one worker

- **Status:** accepted; **thresholds are starting points, not measurements**
- **Date:** 2026-11-03

## Context

The shell restarts the whole engine process if it dies (the outer ring). Inside the engine, a crashed worker made
the health report FAILED and nothing restarted it, and an engine that could not keep up dropped frames silently
until someone noticed. The plan asks for the inner ring: shed load in a defined order, say so, come back; and
replace one worker without restarting the rest.

## Decision

### Degradation ladder (`pipeline/degradation.py`)

| Step | Name | Effect |
|---|---|---|
| 0 | normal | everything at its configured rate |
| 1 | reduced rate | every live camera's inference rate scaled by 0.5 |
| 2 | priority cameras only | scaled by 0.25; live cameras below the top priority are **suspended** (they keep capturing; they are not analysed) and show ANALYSIS SUSPENDED with the reason |

- **Pressure** is analysis dropping at least 5 frames in one second, or the detector's recent p95 latency above
  1500 ms. **Escalation** needs it to last 10 s; **recovery** needs 60 s of calm, one step at a time. Alternating
  pressure never moves the ladder.
- Replayed files are never scaled or suspended: a replay that skips frames is not reproducible.
- The state, reason and suspended cameras are in the telemetry snapshot, `/system/health`, the health issues
  (level DEGRADED), and a banner on every console screen.
- The decision is a pure state machine (`Ladder`), tested without threads or a clock.

**The numbers are defaults chosen so a healthy machine never trips them and a clearly overloaded one does. They
have not been tuned against a measured overload run on the target hardware.** Tune them with `vigil bench` and a
soak; the ladder reports its reason each time it moves, so a bad threshold is visible.

### Restarting a worker (`pipeline/supervision.py`)

- Restartable: the capture worker of a **live** camera (a new source is built; the stream epoch continues so
  downstream state resets), and the watchdog, sampler, pre-roll, retention, stream and degradation workers.
- **Not restartable, and the answer says "restart the engine":** inference (owns the GPU and the batch in
  flight), analysis (every camera's tracking state), response (confirmed events not yet stored), and a replayed file.
- **A thread cannot be killed.** The old worker is asked to stop and joined for 3 s; if it is still running, no
  second copy is started and the refusal says so.
- At most 3 restarts per worker per 10 minutes, and 5 s between them. After that a restart is not fixing it.
- `POST /system/workers/{name}/restart` needs the administrator role, is audited, and the console asks first.

## Consequences

- A stalled worker is now recoverable without restarting monitoring, but only where it is safe.
- Found by the tests: restarting a capture worker for a camera that was OFFLINE or FAILED crashed, because the
  state machine requires such a camera to pass through INITIALIZING; the rebuild now does so.
- Suspended cameras are excluded from event verification (their state is "unhealthy"), so a suspended camera
  produces no incidents. That is the point, and the banner says it.

## Not done

Automatic restart of a crashed worker (this is operator-initiated), and any restart of the inference worker.
