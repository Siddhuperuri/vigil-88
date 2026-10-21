# smoke

**Not implemented. This module detects nothing and never runs.**

- **Event type:** `SMOKE`
- **Subject:** none — it emits no observations
- **Requires:** `CLASSIFICATION`, which **nothing in this build can grant**
  (`core.capabilities.UNGRANTABLE`)
- **Status on every camera:** `UNAVAILABLE`, with the reason and what would have to exist
- **Decision record:** [ADR 0015](../../../../../docs/adr/0015-p6-perception-and-hardening-decisions.md)

The licence and provenance reasoning is the same as [`fire`](../fire/README.md) and is not
repeated here. What follows is what is different about smoke, because it is the harder of the two
and the difference changes what "shipping it" would mean.

## Why smoke is harder than fire

Fire has an appearance. Smoke has an *effect*: it is defined by what it does to the scene behind
it. Contrast falls, edges soften, a region becomes slowly less like itself while drifting and
growing without a clear boundary. None of that is visible in a single frame — a still of thin
smoke is close to a still of a soft-focus region.

The consequence is concrete. A fire classifier can be trained and evaluated on **stills**, and
the public fire/smoke datasets are overwhelmingly stills. A smoke detector worth trusting has to
be trained and evaluated on **video**, over tens of seconds, because the discriminating evidence
is temporal. So the dataset gap for smoke is sharper than "no labelled data": the kind of
labelled data that exists is the wrong kind.

This is also why the architecture puts smoke on a `region:` subject with temporal accumulation
rather than a per-frame verdict (06 §3). The platform already owns the temporal half of that
problem — the log-odds accumulator with half-life decay would be the right consumer of a
per-region per-frame signal. It is the signal that is missing, not the machinery.

## The false positives a real smoke module would have to beat

Recorded so whoever builds it starts from the problem. Each is something that lowers contrast and
drifts, which is the whole of the naive signal:

| Looks like smoke | Why it fools a naive detector |
|---|---|
| Steam from a vent, a kettle, a manhole, a van's exhaust | Physically the same optical effect; the only difference is context |
| Breath on a cold morning | Small, repeated, right in front of the camera |
| Fog, mist, haze, drizzle | Scene-wide contrast loss that a region-based score reads as a very large detection |
| Dust from a vehicle, a swept floor, a building site | Drifts, grows, lowers contrast, lasts minutes |
| A dirty, fogged, wet or spidered lens | Constant, so it reads as smoke that never clears |
| Auto-exposure hunting, a slow focus pull | Global softening that arrives in under a second |
| Direct sun flare, headlights into the lens | Washes out a region with a soft boundary |
| Shadow of a cloud crossing the scene | Large, soft-edged, moving at smoke-like speeds |

The scene-stability validator (06 §6) would catch some of the camera-side ones, which is an
argument for building this on the platform rather than standalone — but it would catch none of
the steam, dust or fog, because the camera is behaving perfectly in all of those.

## What it would take to ship this properly

1. Weights whose licence permits this use, from a named upstream, with a published evaluation —
   as for `fire`.
2. **Labelled video**, not stills, including the hard negatives above, and measured precision and
   recall on a held-out set of it. This is the binding constraint.
3. A `region:` subject feeding per-area accumulators.
4. 06 §7's merge rule, so a `SMOKE` region overlapping a `FIRE` region within
   `arbitration.merge_window_ms` yields one `FIRE` incident with smoke as a contributing signal
   rather than two incidents for one fire. **That rule is implemented and unit-tested** in
   `verification/arbitration.py`; it has no producer in this build.

## Behaviour of what is here

`observe()` **raises** `CapabilityError`. It does not return an empty sequence, because an empty
sequence is indistinguishable from "no smoke in this frame" and that is a claim this module
**cannot** make. The capability gate means it is never called; binding it logs a warning, because
if it is ever bound, something upstream has gone wrong.
