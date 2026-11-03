# D3 report: advanced

**Branch:** `feat/d0-foundation` (D0 to D3 together) · nothing pushed or merged.

## Scope, and what was left out

The plan's D3 list is: timeline, candidate feed, command palette, false-positive review loop, line-crossing zone
and module, single-incident bundle, per-camera overrides, optional binary-frame overlays. Windows Hello and
evidence encryption are marked "not promised". Five of the first six are built; the rest are **not**, and each
is named on screen (System, "Not built in this version") rather than left out silently.

| Feature | State | Notes |
|---|---|---|
| Timeline | Built | Incidents and camera state changes on one newest-first list; 1 hour, 24 hours or 7 days |
| Candidate-rejection feed | Built | Which checks refuse the most, and each candidate with outcome and reason; says a candidate is not an incident |
| Command palette | Built | Ctrl+K; go to a view or run an action; lists only what the role may use |
| False-positive review loop | Built, with a stated limit | Fixed dismissal reasons (ADR 0018), required by the console prompt, counted per reason in Reports. The rate stays "dismissal rate", never "false-positive rate" |
| Single-incident bundle | Built | Zip with the incident, the evidence files and a manifest marking each `ok`, `mismatch` or `missing` |
| What each camera can detect | Built | Per camera, the engine's own module availability, reasons and caveats, with "detection is unmeasured" stated first |
| Line-crossing zone and module | **Not built** | A line is not a polygon and nothing would evaluate it. Offering a zone that does nothing is what the rules forbid |
| Per-camera threshold overrides | **Not built as a screen** | `module_params` in a camera file already works and the console preserves it when saving a camera, but cannot edit it |
| Binary-frame overlays in the console | **Not built, deliberately** | The plan said "only if server overlays prove limiting". No measurement showed that |
| Windows Hello, evidence encryption | **Not built, not designed** | Needs a key-management design this project does not have |

## What was verified

- **Engine:** 2,124 tests (2 skipped for hardware), including the new ones for the vocabulary (including a reason that merely contains
  a code), the API refusing an unknown code and leaving the incident open, counted reasons that reconcile with
  the dismissal total, and the bundle with one `ok`, one tampered and one missing file (the tampered file is
  included so it can be examined; the missing one is listed, not invented), an empty bundle, and an unknown
  incident.
- **Console:** 157 component tests, with axe for every new screen, and for the dismissal prompt (the first reason
  takes focus, so the keyboard path is `d`, arrows, Enter; Escape cancels from the list; the confirm button is
  disabled with its reason until a reason is chosen; a failed reason list blocks dismissal and says why).
- **Real app:** the Electron test opens the palette with Ctrl+K, runs axe on it at WCAG 2.2 AA including colour
  contrast, cancels with Escape, then navigates by typing.

## Defects found on the way

- A gate failure showed a real race in my own end-to-end test (the `?` key was pressed before the incident desk
  had registered it); the test now waits for the desk.
- The first plan to say per-camera thresholds were "not built" was wrong: they exist as `module_params`. The
  on-screen text was corrected before it shipped.

## Not verified

- **The review loop has never run on real operator use.** The exit criterion is "a dismissal rate per module
  version from real operator use". The mechanism is tested; no operator has dismissed real incidents through it,
  so no rate has been produced.
- **The bundle was not opened by an independent tool**, only read back with Python's `zipfile`. It was not tried
  with a multi-gigabyte clip, and it is not signed.
- **The command palette has not been used with a screen reader.** The NVDA pass is still outstanding.
- The earlier gaps stand: RTSP against a real camera, the real-GPU spike, live apply without a restart.
