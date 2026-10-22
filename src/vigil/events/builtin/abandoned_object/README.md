# abandoned_object

A stationary object of a watched class that a person was seen beside and then left.

- **Event type:** `ABANDONED_OBJECT`
- **Subject:** `track:<id>` — one hypothesis per object
- **Requires:** `DETECTION`, `TRACKING`
- **Optional:** `ZONES` — with it the module watches only the configured zone kinds; without it
  it watches the whole frame, runs DEGRADED, and says `zones_unavailable` in its facts rather
  than quoting a location it cannot know

## The distinction this module exists to make

**Stationary is not abandoned.** A bin is stationary. A parked bicycle is stationary. A potted
plant has been stationary for years. A module that fired on "stationary object detected" would
fire on all of them, on every camera, forever — which is the fastest available way to teach an
operator to ignore this system.

So the hypothesis has three parts and all three must hold:

1. **A watched class, judged `STATIONARY` by the tracker** — still for
   `tracking.stationary_min_duration_ms`, not merely slow or small.
2. **No plausible owner near it now.** "Near" is measured in the *object's own box heights*
   (`owner_distance_ratio`), so one threshold means the same thing for a bag two metres away and
   one thirty metres away. Only *visible* owners count: a predicted box is the filter's guess,
   and "someone might be standing there" is not evidence that the bag is attended.
3. **An owner was near it, and then left**, for at least `separation_after_ms`. This is the part
   that separates a dropped bag from the furniture.

That third condition is the sequence 06 §11 names: `object present -> owner nearby -> owner gone
-> object persists`. It is expressed as accumulating evidence rather than as a
`temporal/sequence.py` pattern deliberately: a matcher must be fed ordered marks and would reset
the first frame the tracker blinked, whereas "the last time an owner was seen beside it" survives
a detection gap. The matcher remains the right tool for patterns whose steps are discrete events;
here the steps are continuous states.

**Furniture is handled honestly rather than cleverly.** An object the module has never seen an
owner beside is furniture as far as it can tell, so it stays silent — permanently, with no list
of known objects and no learning. `owner_never_observed` appears in the facts. The cost is the
matching real failure, below.

## How the signal is built

| Term | Effect | Why |
|------|--------|-----|
| `base_signal` | 0.60 | Stationary, unattended, and separated long enough to count |
| `separation_bonus_ratio` | +0.15 | The owner has been gone past `separation_after_ms` |
| `persistence_bonus_ratio` | +0.15 after `persistence_after_ms` | Still there half a minute later. Persistence is evidence |
| `attended_signal` | 0.15 | A plausible owner is beside it: evidence **against** |

The attended signal is the one that earns its keep. Emitting evidence against while someone is
with their bag is what makes an event release promptly when an owner comes back, instead of
waiting for the accumulator to decay (04 §6.1).

Otherwise the module emits **nothing** — not a weak signal. A trickle of weak evidence about the
bins would eventually confirm one.

## Known false positives

- **Deliberately placed objects.** Stock left on a loading dock, a delivery, a market stall, bins
  put out for collection, luggage set down while someone waits. All geometrically identical to an
  abandoned bag. `zone_kinds` and `EXCLUSION` zones are the practical answer, plus operator mutes
  for a known delivery window.
- **An owner standing just outside `owner_distance_ratio`.** Someone a few metres from their own
  suitcase reads as separated. Widening the radius trades this against missing a genuinely
  abandoned object in a crowd; there is no setting that avoids both.
- **An owner the detector does not see** — seated, occluded, in a doorway, or simply missed. The
  object then reads as unattended while its owner is a metre away.
- **An owner who changes identity.** A track lost and re-acquired is a *new* track id, and the
  ownership history is keyed by id, so the separation clock effectively restarts from the first
  frame the new track was seen beside the object. This can delay a real event.
- **Detector confusion on bags.** `object.bag` and `object.box` are the least reliable classes in
  the COCO mapping (docs/LIMITATIONS.md). Rucksacks on backs, handbags held low and dark objects
  on dark ground are all unreliable, in both directions.
- **One object detected as two**, or two bags detected as one, each producing or suppressing
  evidence that does not correspond to an object.

## What it cannot do

- **No re-identification.** Ownership is proximity in time and space, nothing more. It cannot
  tell whose bag it is, and cannot follow an owner who left the frame and came back (01 §2).
- **No object that predates the camera.** A bag dropped before this track existed — before
  startup, before a reconnect, or before the tracker re-acquired it — has no observed owner, so
  condition 3 never holds and the module stays silent. This is the cost of the furniture rule,
  and it is the same mechanism: the module cannot tell a bag nobody has ever stood beside from a
  bin.
- **Nothing across an epoch change.** A reconnect or a tracker reset clears ownership history,
  because track ids no longer mean what they meant (04 §3.2). Objects visible across the gap need
  a new owner sighting before they can be reported again.
- **No judgement about contents or threat.** It reports a persisting state, not a hazard.
