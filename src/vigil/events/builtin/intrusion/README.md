# intrusion

A tracked person present inside a restricted zone while that zone is active.

- **Event type:** `INTRUSION`
- **Subject:** `track:<id>` — one hypothesis per person, so two people in one zone are two
  events and the incident can name who (06 §3)
- **Requires:** `DETECTION`, `TRACKING`, `ZONES`
- **Optional:** `VELOCITY` — without it the module runs DEGRADED and applies no inwardness
  bonus; the caveat travels with every event it produces

## What it detects

Per analysed frame, for each active zone whose `kind` is in `zone_kinds`, for each track the
scene analyzer reports as occupying that zone, the module emits one `Observation` whose signal
is built from:

| Term | Effect | Why |
|------|--------|-----|
| `base_signal` | 0.70 | Plain, unambiguous presence. Not 1.0: one frame must not dominate |
| `dwell_bonus_ratio` | +0.10 after `dwell_bonus_after_ms` | Persistence is evidence; someone still there after seconds is less likely to have strayed in |
| `velocity_bonus_ratio` | up to +0.15 | Moving deeper into the zone supports intrusion more than moving along or out of it |
| `edge_penalty_ratio` | −0.15 within `edge_margin_ratio` of the boundary | Boundary flicker is this module's main false positive |

The result is clamped into `[0.51, 0.95]`. The floor matters: a penalised-but-real presence is
weak evidence *for* an intrusion, never evidence against one.

**The module does not decide anything.** "Person enters restricted zone" is not "confirmed
intrusion". The observation goes to the temporal engine, which decides whether the evidence
accumulates past `temporal.l_activate` while also satisfying `min_observations`,
`min_support_ratio` and `min_duration_ms` (04 §6.3). With the shipped defaults that means
roughly 1.2 s of sustained support before anything is promoted, and a brief boundary crossing
produces no candidate at all.

## What it cannot do

- **No intent.** It cannot distinguish a trespasser from an employee who forgot their badge,
  or from a contractor who is meant to be there. That is what zone schedules, `EXCLUSION`
  zones and operator mutes are for.
- **No identity.** There is no re-identification and no facial recognition (01 §2). A person
  who leaves and returns is a new track and a new event.
- **No cross-camera reasoning.** One camera's intrusion is independent of another's.
- **No climbing, loitering or tailgating semantics.** It reasons about presence, dwell and
  direction. "Climbed the fence" is not a thing it can see.
- **Nothing without zones.** A camera with no zone file has no `ZONES` capability, so the
  module is UNAVAILABLE on it and says so, rather than running and finding nothing.

## Known false positives

Stated so they can be argued with, measured and tuned. All of these have been reasoned about;
none has yet been measured against real footage on this hardware.

1. **People legitimately passing near a boundary.** A path running alongside a restricted yard
   produces repeated near-boundary presence. Mitigations: `edge_penalty_ratio`, a tighter
   polygon, or an `EXCLUSION` zone over the path. This is the most likely source of real-world
   noise.
2. **A reflective surface or a screen showing people.** A window, a monitor or a glass door can
   produce persistent, well-tracked "people" that genuinely are inside the polygon as far as
   the geometry is concerned. Only an `EXCLUSION` zone fixes this.
3. **Detector class flicker.** A person intermittently classified as `UNKNOWN` loses support on
   those frames. `min_support_ratio` tolerates some of this; heavy flicker suppresses a real
   event rather than inventing one, which is the safer direction but is still a miss.
4. **Tracker identity switches.** Two people crossing can swap ids, splitting one person's
   evidence across two accumulators so neither confirms. `MotionSanityValidator` catches the
   implied teleport; the evidence is still fragmented.
5. **Shadows and headlights at night.** A moving shadow can be detected as a person by some
   weights. Nothing in this module can tell the difference; it is a detector-quality matter.
6. **An anchor-point disagreement at a boundary.** The module scores distance from the foot
   point, matching `scene.zone_anchor`'s default. If that setting is changed to `center` without
   this module agreeing, containment and the edge penalty will disagree by half a box height.

## Tuning

Per-camera overrides go in `cameras/<cam>.yaml` under `module_params.intrusion`; defaults for
every camera go in `config/modules/intrusion.yaml` (06 §5).

The parameter that trades false positives against detection latency is not in this module: it is
`temporal.min_duration_ms`. Raising it suppresses brief crossings and delays every real
detection by the same amount. Measure both before changing it.

**No accuracy figure is claimed here.** The tests prove the mechanism behaves as specified on
synthetic scenes. The real false-positive rate for a given camera is an empirical question,
answerable only from that camera's footage and the dismissal rate instrumented from P4
(09 §11).
