# line_crossing

A tracked object crossed a tripwire in the direction that counts.

- **Input:** `scene.lines` (the `lines:` list of a camera's zone file) and each track's own trajectory.
- **Event type:** `restricted_area_entry`. Arbitration already suppresses it while an `intrusion` is active for
  the same subject.
- **Requires:** detection, tracking and zones. Velocity is optional and only adds a bonus.
- **Direction:** `any`, `to_right` or `to_left`, as seen walking from the line's `start` to its `end`.
- **Reported for `lookback_ms` after the crossing**, read from the trajectory, so the module keeps no timer and no
  "already fired" flag and accumulation has more than one frame to work with.

```yaml
# config/zones/front-gate.zones.yaml
version: 1
lines:
  - line_id: back-door
    start: [0.20, 0.55]
    end: [0.80, 0.55]
    direction: to_right
    criticality: 2
```

Enable it with `modules.enabled: [line_crossing]`. Discovery never enables a module.

## What it is not

Geometry over tracks, nothing more. It has been exercised on synthetic scenes only, never on real footage, and its
accuracy is the detector's and the tracker's. Counting how many crossed is not built.
