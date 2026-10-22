# fire

**Not implemented. This module detects nothing and never runs.**

- **Event type:** `FIRE`
- **Subject:** none — it emits no observations
- **Requires:** `CLASSIFICATION`, which **nothing in this build can grant**
  (`core.capabilities.UNGRANTABLE`)
- **Status on every camera:** `UNAVAILABLE`, with the reason and what would have to exist
- **Decision record:** [ADR 0015](../../../../../docs/adr/0015-p6-perception-and-hardening-decisions.md)

## Why this directory exists at all

An absent module is invisible. Someone reading `events/builtin/` sees three directories, finds
`FIRE` in `EventType`, and has to work out for themselves whether fire detection is configured,
broken, or missing. So the gap is declared instead: the module is discovered, it is listed in the
API and the console, and it reports UNAVAILABLE with a reason an operator can act on. The
capability negotiation of D-008 is the mechanism the architecture already has for exactly this,
and using it is cheaper and more honest than a sentence in a document nobody opens.

## Why there is no detector

P6 looked for a model and did not find a usable one. The full candidate list and each
disqualification are in ADR 0015; the summary:

- **The accurate models are AGPL-3.0.** Essentially every credible fire/smoke detector is an
  Ultralytics YOLO derivative. ADR 0012 refused Ultralytics for this system because the AGPL
  network clause is triggered by precisely what it does — serving a UI — and because it drags in
  PyTorch. A fine-tune does not escape the licence of the thing it was fine-tuned from.
- **One candidate was permissive and still unusable.** `prithivMLmods/Fire-Detection-Engine-ONNX`
  is Apache-2.0 and genuinely ONNX, but it is a whole-image three-class scene classifier, its
  training data is unnamed and unlicensed, and the precision figures on its card are not stated
  to come from a held-out split. It also cannot localise, so it could not populate the `region:`
  subject the architecture's fire module is built around.
- **Several carry no licence at all**, which is not permission.
- **We could not evaluate any of them.** There is no labelled fire or smoke data in this
  repository, so measuring a candidate ourselves was not available either.

10-roadmap §9's exit criterion allows exactly two outcomes: a measured precision and recall on a
held-out set, **or** the module is not shipped and `LIMITATIONS.md` says why. This is the second,
and what ships is the declaration, not the capability.

## Why not a colour heuristic

Fire is orange and it flickers, so a hue-plus-flicker score is an afternoon's work. It would also
fire on sunsets, brake lights, hi-vis jackets, sodium street lighting, a space heater and a
television — and those **false positives** are not a tuning problem, because the heuristic has no
representation of the thing that distinguishes them. The honest limitation is better than a
control an operator must learn to disbelieve: a detector with an untrustworthy alarm rate is worse
than no detector, because it also consumes the attention the real alarms need (README rule 1).

The list of false positives a real fire module would have to beat, recorded here so whoever
builds it starts from the problem rather than from the architecture:

| Looks like fire | Why it fools a naive detector |
|---|---|
| Sunset, sunrise through a window | Large warm-hued region, slow movement |
| Brake lights, indicators, emergency beacons | Saturated red/orange, periodic flicker at fire-like frequencies |
| Hi-vis jackets, traffic cones, safety barriers | Exactly the hue band, and they move like people |
| Sodium and incandescent lighting, heaters | Warm, steady, often with a flickering filament |
| A screen or television | Arbitrary content including footage of fire |
| Reflections of any of the above in glass or wet ground | Doubles every one of them |

Smoke's own confusions are in [`../smoke/README.md`](../smoke/README.md).

## What it would take to ship this properly

1. Weights whose licence permits this use, from a named upstream, with a published evaluation.
2. A labelled held-out set with both positives and the hard negatives above, and measured
   precision and recall on it — the exit criterion, not an aspiration.
3. A classifier engine inside the VRAM budget alongside the detector, with the existing guard
   enforcing it (10-roadmap §9's secondary-model budgeting).
4. A `region:` subject so the temporal engine accumulates per area, and 06 §7's merge rule to
   join a `FIRE` and a `SMOKE` region into one incident. **The merge rule is implemented and
   unit-tested** (`verification/arbitration.py`); it has no producer in this build.

## Behaviour of what is here

`observe()` **raises** `CapabilityError`. It does not return an empty sequence, because an empty
sequence is indistinguishable from "no fire in this frame" and that is a claim this module
**cannot** make. The capability gate means it is never called; binding it logs a warning, because
if it is ever bound, something upstream has gone wrong.
