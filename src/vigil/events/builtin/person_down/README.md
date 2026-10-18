# person_down

A tracked person whose bounding-box geometry has collapsed from upright to prone and stayed
that way.

- **Event type:** `PERSON_DOWN`
- **Subject:** `track:<id>` — one hypothesis per person
- **Requires:** `DETECTION`, `TRACKING`
- **Optional:** `VELOCITY` — without it the module runs DEGRADED, the immobility term is
  dropped and the remaining weights are renormalised; the caveat travels with every event

## What it is, said plainly

**This module is geometry. It is not pose estimation, and it makes no claim about injury.**

A camera can show that something which was tall and moving has become short, wide and still.
That is what a fall looks like from a fixed camera, and it is worth telling an operator about.
What it cannot show is *why*. `PERSON_DOWN` here means "a person's posture appears to have
collapsed and stayed collapsed" — not injured, not unconscious, not in medical distress. The
descriptor, the facts on every observation (`method: bounding-box geometry; no pose model`) and
this file all say the same thing, because the one failure mode that would matter most is an
operator believing this is a medical alarm.

No `POSE` capability is declared, because none is used. 10-roadmap §10 defers pose estimation
until a module needs it, and this module exists to make that decision on evidence. Declaring
`POSE` as optional-but-unused would be worse than not declaring it: capability negotiation would
report a degradation that changes nothing, which is how D-008 decays into theatre.

## How the signal is built

Three terms, each a pure function of the scene, weighted (the weights must sum to 1.0) and
mapped into `[signal_floor, signal_ceiling]`:

| Term | Weight | Reads | Why it is evidence |
|------|--------|-------|--------------------|
| aspect ratio | 0.4 | the current box | A standing adult's box is roughly 0.3–0.5 wide-over-tall; a lying one is at or above 1.0 |
| verticality collapse | 0.4 | the track's own trajectory | Height falling to a fraction of what *this track* used to be needs no knowledge of camera geometry or distance, which an absolute size would |
| immobility | 0.2 | velocity in box heights per second | Someone who collapsed and stayed put differs from someone who crouched to tie a lace |

Plus `stillness_bonus_ratio` once `stationary_duration_ms` passes
`stillness_bonus_after_ms`. That term matters because it is the only one that can speak to
minutes: the trajectory is a bounded ring buffer, and `stationary_since` is not.

**A missing term is renormalised, never zeroed.** With no velocity the other two decide, each
carrying proportionally more weight. Treating an uncomputable term as 0 would make absent
evidence into evidence *against*, which is a different and wrong statement.

**The band straddles neutral.** An upright, walking person produces a signal *below* 0.5 — real
evidence against the hypothesis (04 §6.1). That is deliberate: it is what makes an event release
promptly when someone stands up, instead of waiting for decay. A genuinely ambiguous posture
resolves to just below neutral rather than at it, because "I cannot tell" is an absence of
support, not support.

**The module decides nothing.** One frame of odd geometry is not a confirmed event. Promotion
needs `temporal.l_activate` to be crossed while `min_observations`, `min_support_ratio` and
`min_duration_ms` are all satisfied (04 §6.3), and then eleven validators have to agree.

## Known false positives

These are the ones to expect, and none of them is fixed by tuning alone:

- **Sitting on the ground, lying down deliberately, sunbathing, stretching, yoga.** Geometrically
  identical to a collapse. This is the dominant false positive and the reason the module ships
  after the verification layer rather than before it.
- **Crouching, kneeling, bending to pick something up.** Usually rejected by
  `min_duration_ms`, because the posture does not persist — but a long crouch will not be.
- **A person partly occluded at the bottom of the box** (behind a car, a counter, a parapet).
  The box shortens without the person moving, which reads as collapse. An `EXCLUSION` zone over
  the occluding furniture is the practical answer.
- **A person at the frame edge**, half out of view: the box is clipped to the frame, so height
  collapses while they simply walk out of shot.
- **Two people merging into one detection**, then separating: the box width doubles and the
  aspect ratio inverts without anyone falling.
- **A camera that is not level**, or a steep downward view. A near-overhead camera sees every
  person as wide and short, so the aspect term is near-saturated for everyone standing. On such
  a camera this module should be left out of `modules.enabled` rather than retuned.
- **Prams, trolleys, luggage and dogs** misclassified as `person`, which are wide and low by
  nature.

## What it cannot do

- **No injury assessment, and no triage.** See above. It cannot rank two people on the ground.
- **No fall detection as an event.** It reports a *state* that persists, not the transition. A
  person who falls and gets straight up produces no event, by design.
- **No standing reference for someone first seen already down.** Then `collapse` is reported
  unavailable (`collapse_unavailable` appears in the facts) and the aspect and immobility terms
  decide. This is a real gap: a person already on the ground when the camera comes online is
  harder for this module to see than one who falls in view.
- **No depth, so no absolute size.** Everything is relative to the track's own history, which is
  why an occlusion that changes the box reads as a posture change.
- **Nothing about the person's identity** (01 §2).
