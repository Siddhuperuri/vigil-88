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

## False positives and limitations

Geometry over tracks, nothing more. It has been exercised on synthetic scenes only, never on real footage, and its
accuracy is the detector's and the tracker's. Counting how many crossed is not built.

Known false positives, written down so nobody has to discover them:

- **Two people, one track.** When the tracker swaps two identities as they pass each other, the "crossing" belongs
  to a track that did not cross. This is the tracker's failure, reported here as the module's.
- **A shadow, a reflection or a large box.** The foot point is the bottom centre of the box. A box that
  grows when a person raises their arms, or that includes a shadow, moves its foot point across a line the
  person did not.
- **Standing on the line.** A person who stands astride the line flickers between sides. Each flicker is a
  crossing in some direction; with `direction: any` that is a stream of observations until they move off.
- **A line drawn on a ground plane the camera sees obliquely.** A line across a doorway is straight in the
  picture and may not be where the floor boundary is. Draw it where the feet cross, not where the door is.

It **cannot** tell who crossed, or whether they were allowed to. Counting how many crossed is not built.
