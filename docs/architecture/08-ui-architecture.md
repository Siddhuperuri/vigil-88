# 08 — UI Architecture

> Status: DESIGN. No code exists. The console is named **VIGIL-88 Console**.

## 1. What this interface is for

An operator watching this screen has one job: notice what matters, and act on it. Every
decision below serves that and nothing else.

What it is **not**: a dashboard of metrics, a CRUD admin panel, a template with cards and
rounded corners, or a demo of the detector's abilities. The design test for any element is
**"what does the operator do differently because of this?"** If the answer is nothing, it is
removed. A number that is never acted on is noise competing with a number that is.

Concretely, this rules out: decorative sparklines on incident cards, a "total detections
today" counter, animated gradients, a welcome panel, confidence shown to three decimals, and
a map view of three cameras in one building.

## 2. Information hierarchy

Ranked by the cost of missing it. Screen position, size, and contrast follow this ranking
exactly:

1. **An unacknowledged CRITICAL/HIGH incident** — must be impossible to miss anywhere in
   the app, including in a different view.
2. **Live camera views** — what is happening now.
3. **Open incident queue** — what needs attention.
4. **Camera health** — whether what I am seeing is trustworthy.
5. **Recent event timeline** — context for what is happening now.
6. **System health** — whether the system is keeping up.
7. **Historical analytics** — reviewed, not monitored.

Point 4 outranking the timeline is deliberate: a blind camera shown as a black tile with no
explanation makes every other judgement unsound. Degraded and suspended cameras are
labelled on the tile itself, in words.

## 3. Shell and views

```
+----------------------------------------------------------------------------------+
| VIGIL-88  [ARMED]   3/3 CAMERAS   GPU 41%  2.1GB   14.2 FPS  p95 62ms   02:41:07 |  48px status strip
+----+-----------------------------------------------------------------------------+
| [] |                                                                             |
| [] |                           active view                                       |
| [] |                                                                             |
| [] |                                                                             |
| [] |                                                                             |
+----+-----------------------------------------------------------------------------+
 56px rail
```

**Status strip** (always visible, never scrolls): armed state, camera count with degraded
count, GPU utilisation and VRAM, pipeline FPS, inference p95, wall clock. Any active mute
appears here with a countdown (06 §10). When the system is `OVERLOADED` or any camera is
`FAILED`, the strip takes a severity border — this is the one piece of global chrome allowed
to change colour.

The clock is in the strip because every incident timestamp is meaningless without knowing
what "now" is, and because a frozen clock is the fastest way to spot a hung UI.

| View | Purpose | Primary element |
|------|---------|-----------------|
| **Live Wall** | Watch | Adaptive camera grid, annotated streams, incident flash overlay |
| **Incident Desk** | Act | Priority-sorted open incidents; evidence and explanation side by side |
| **Timeline** | Understand | Dense horizontal time axis, one lane per camera, incidents and health events on the same axis |
| **Cameras** | Diagnose | Per-camera health, fps/latency history, reconnect log, module availability |
| **Zones** | Configure | Polygon editor on a live still, per-zone kind/criticality/schedule |
| **Analytics** | Review | Incidents by type/camera/hour, dismissal rate, detection-latency distribution |
| **System** | Trust | Stage latencies, queue depths, GPU/CPU/memory series, degradation history, model and config identity |

Seven views, each with one job. The Timeline putting camera health events on the same axis
as incidents is the single most useful correlation in the product: *"the camera was
reconnecting when that fired"* is the answer to a large fraction of "why did this fire"
questions.

**Analytics shows dismissal rate per module version**, because that is the only honest
measure of whether false-positive suppression works (06 §9).

## 4. Video transport — and the overlay synchronisation problem

This needs stating plainly because the obvious approach is broken.

**The obvious approach:** stream MJPEG into an `<img>`, push detections over the WebSocket,
draw boxes on a `<canvas>` on top. Toggleable overlays, crisp vectors, no re-encoding.

**Why it does not work:** the two channels have independent, variable latency, and a
browser `<img>` tag exposes neither the current frame's identity nor its timestamp to
JavaScript. There is no way to know which frame is on screen, so there is no way to pair it
with the right detections. The result is boxes that lag or lead the video by a variable
100–500 ms — which looks like a *detection* bug and will be debugged as one, repeatedly.

**The decision:** server-side annotation is the default for live video.

| Context | Approach | Why |
|---------|----------|-----|
| Live Wall, Cameras | **Server-side annotated MJPEG.** Boxes, zones, and labels drawn into the frame by the worker that already holds both the pixels and the detections | Synchronised by construction. Impossible to desync |
| Incident review, Zones editor | **Still image + client-side canvas overlay** | The client fetches a frame and its exact `DetectionSet` together, so pairing is correct. Vector overlay allows zoom, toggling, and polygon editing |

Overlay toggles on the live stream are a real server-side query parameter —
`GET /api/v1/cameras/{id}/stream.mjpeg?overlay=boxes,zones,tracks,labels` — and the server
re-encodes accordingly. The control does what it says (README rule 1); it is not a
client-side illusion.

**Encode-once fan-out.** Annotation and JPEG encoding happen once per camera at
`api.stream_fps` (default 10) in the response worker, and the encoded bytes are fanned out
to every connected viewer. N browser tabs cost one encode, not N. Without this, opening the
console twice would double CPU load and get blamed on the pipeline.

**No camera is streamed unless someone is watching it.** Streams are reference-counted and
the encoder stops when the last viewer disconnects. `MJPEG` at 10 fps and quality 70 is
roughly 1.5–3 Mbit/s per camera over loopback, which is free; the encode is not.

If measurement at P1 shows MJPEG cannot sustain the wall at the camera count we need,
WebRTC goes behind the existing `VideoTransport` protocol (open question Q5). The protocol
exists from P0 so that is a swap, not a rewrite.

## 5. State ownership

```
  SQLite  ----REST---->  TanStack Query cache  ---->  views        (history, config)
  Pipeline ---WS------>  telemetry store (zustand) -->  views      (live state)
  Browser  ----------->  ui store (zustand)       -->  views       (layout, filters, selection)
```

Rules:

1. **The console holds no authoritative state.** It renders what the server reports. There
   is no client-side incident list that can diverge from the database.
2. **The console performs no inference, ever.** No TensorFlow.js, no WASM models, no
   client-side image analysis. Canvas overlay drawing is rendering, not inference.
3. **One WebSocket**, carrying a discriminated union of message types, validated with zod on
   arrival. A schema-mismatched message logs an error and is dropped; it never silently
   corrupts a view.
4. **Server pushes are coalesced** at `api.push_interval_ms` (default 100). The console
   needs current state, not every intermediate state (01 §9).
5. **Optimistic updates only for operator actions** (acknowledge, annotate), reverted on
   failure with a visible error. Never for pipeline state.
6. **Types are generated** from the backend's OpenAPI schema into `ui/src/lib/types.gen.ts`
   by `scripts/gen_api_types.ps1`, and that file is never hand-edited. A backend field rename
   becomes a TypeScript compile error rather than a runtime `undefined` in a view.

### WebSocket message types

```typescript
type Telemetry =
  | { kind: "camera_state";  cameraId: string; state: CameraState; detail?: string }
  | { kind: "camera_stats";  cameraId: string; fps: number; latencyMs: number; skipped: number }
  | { kind: "detections";    cameraId: string; frameIndex: number; items: DetectionDTO[] }
  | { kind: "incident_new";      incident: IncidentDTO }
  | { kind: "incident_updated";  incident: IncidentDTO }
  | { kind: "candidate_rejected"; cameraId: string; eventType: string; reason: string }
  | { kind: "system_metric"; metric: SystemMetricDTO }
  | { kind: "degradation";   step: number; reason: string; active: boolean }
  | { kind: "module_state";  cameraId: string; moduleId: string; state: ModuleState; reason?: string };
```

`candidate_rejected` is on the live channel on purpose. During tuning, watching rejections
stream past with their reasons is the single most informative view in the system, and it is
how an over-aggressive validator gets caught.

## 6. Visual identity

The identity is **instrument panel**: a precision measuring device, not a web app. Dense,
hairline-ruled, monospace-numeric, near-monochrome — with colour reserved almost entirely
for severity.

### Tokens (`ui/src/design/tokens.css`)

```css
:root {
  /* Surfaces — warm-neutral charcoal, deliberately not blue-black */
  --vg-void:        #0a0b0d;   /* page */
  --vg-panel:       #12141a;   /* panels */
  --vg-raised:      #1a1d25;   /* cards, inputs */
  --vg-line:        #262a35;   /* hairlines — the dominant structural element */
  --vg-line-strong: #3a404f;

  /* Text */
  --vg-text:        #e6e9ef;
  --vg-text-2:      #9aa3b2;
  --vg-text-dim:    #6b7383;

  /* Signal — system nominal, live indicators. The ONE non-severity accent. */
  --vg-signal:      #2dd4bf;
  --vg-signal-dim:  #166e63;

  /* Severity — the only other colour in the system */
  --vg-sev-info:     #5a93d4;
  --vg-sev-low:      #8a9bb0;   /* deliberately unexciting */
  --vg-sev-moderate: #e0a93b;
  --vg-sev-high:     #e8762c;
  --vg-sev-critical: #e5484d;

  /* Type */
  --vg-font-ui:   "Inter", system-ui, sans-serif;
  --vg-font-mono: "JetBrains Mono", ui-monospace, "Cascadia Mono", monospace;

  /* 8px grid */
  --vg-s1: 4px;  --vg-s2: 8px;  --vg-s3: 12px; --vg-s4: 16px;
  --vg-s5: 24px; --vg-s6: 32px; --vg-s7: 48px;

  --vg-radius: 2px;             /* near-square: instruments are not pill-shaped */
  --vg-hairline: 1px solid var(--vg-line);
}
```

Craft rules that make it read as an instrument rather than a theme:

- **All numerics are mono with `font-variant-numeric: tabular-nums`.** A FPS readout whose
  digits change width makes the whole strip jitter, and that jitter is what makes a UI feel
  cheap.
- **Hairlines, not shadows.** Structure comes from 1px rules on a dark ground. No elevation
  shadows, no glows, no glass.
- **2px radius.** Near-square corners read as technical; 12px radius reads as consumer.
- **Colour is information.** `--vg-signal` means live-and-nominal. Severity colours mean
  severity. Nothing is coloured for decoration, so any colour on screen carries meaning —
  which is what makes a red border register instantly instead of competing with a palette.
- **Density is a feature.** 13px base, 1.4 line height, 8px gutters. An operator should see
  twelve incidents without scrolling, not four.
- **No empty-state illustrations.** "NO OPEN INCIDENTS" in dim mono, centred. The absence of
  incidents is good news and should be visually quiet.

### Dark and light

The console is dark-first because it is viewed in dim rooms for long periods. A light theme
is provided via `[data-theme="light"]` token overrides for printed reports and bright
control rooms — the same tokens, different values, zero component changes. Severity hues are
re-tuned per theme rather than reused, because a hue that reads as urgent on charcoal reads
as pastel on white.

## 7. Motion

Restrained is a requirement, so the rules are explicit:

| Allowed | Duration | Purpose |
|---------|----------|---------|
| New incident enters the list | 150 ms slide + fade, **once** | Draw the eye to a state change |
| Status strip severity border on first unacknowledged CRITICAL | 2 s pulse, 3 cycles, then static | Demand attention, then stop demanding it |
| View transition | 100 ms opacity | Orientation |
| Value change in a readout | None | A number that animates is a number you cannot read |
| Live indicator dot | 1 s opacity breathe, 0.6 to 1.0 | Proves the stream is alive, not frozen |

Forbidden: looping skeleton shimmer, spinners for anything under 400 ms, parallax, animated
backgrounds, number count-up, carousels, anything that moves while nothing changed.

`@media (prefers-reduced-motion: reduce)` reduces every transition to opacity-only at 0 ms.
The live dot becomes a static glyph.

The reasoning: this screen is watched peripherally for hours. Any persistent motion either
trains the operator to ignore movement — which defeats the one mechanism available for
raising an alarm — or exhausts them. Motion is spent only where it buys attention.

## 8. No fake functionality in the UI

The mechanism that enforces README rule 1 at the interface:

| Situation | Rendering |
|-----------|-----------|
| Module UNAVAILABLE on a camera (04 §4) | Listed greyed, with the missing capability and the fix: *"needs GROUND_PLANE — calibrate in Zones"* |
| Event type with no implementation | Not offered anywhere. Not a disabled filter option, not a zero-valued chart series. Absent |
| Alert channel not implemented | Absent from the channel list; named in `docs/LIMITATIONS.md` |
| GPU metrics with no NVML | "GPU: unavailable (no NVML)". Never 0% |
| Camera `ANALYSIS_SUSPENDED` | Tile shows video with a persistent label "ANALYSIS SUSPENDED — degradation step 4", not a normal tile |
| Feature behind a disabled config flag | Shown with the config key that enables it |
| Action that would fail | Control disabled with a tooltip stating why. Never enabled-then-error |
| Data still loading | Determinate text ("loading 1,284 incidents"), not a shimmer |

A control that is visible and enabled does what it says. If it cannot, it is disabled and
says why, or it is not there.

## 9. Accessibility

Operator safety, not compliance theatre:

- **Severity is never colour alone.** Every severity carries a glyph and a text label.
  Roughly 8% of men have a red/green deficiency, and this system's highest-stakes signal is
  red.
- Text contrast at or above 4.5:1 (AA) on its actual background, verified by a script over
  `tokens.css` in `scripts/check.ps1` — not by eye.
- Full keyboard operation of the Incident Desk: `j`/`k` to move, `a` acknowledge, `d`
  dismiss, `/` search, `?` shortcuts. Hands stay on the keyboard during an incident.
- Visible focus rings on `--vg-signal`. Never `outline: none`.
- `aria-live="assertive"` on the incident announcer region so a new CRITICAL is announced by
  a screen reader.
- Target size at least 32x32 px for any destructive action (dismiss, mute).

## 10. Performance

The console must not be the reason the system feels slow.

| Concern | Approach |
|---------|----------|
| Incident list of 10k+ | Keyset pagination on `confirmed_utc` (05 §5) plus windowed rendering. No full-list fetch |
| Timeline with 50k events | Server-side bucketing to the pixel width requested; the client never receives more points than it can draw |
| WS message storm | Server-side coalescing (§5 rule 4) plus a client-side rAF batch, so React renders at most once per frame |
| Many camera streams | One `<img>` per visible tile; off-screen tiles disconnect their stream (§4) |
| Chart re-render | Inline SVG, memoised on data identity; no chart library (03 §5) |
| Memory over a long shift | Bounded client-side series buffers matching the server's window; the console must survive an 8-hour shift without a reload, which is a testable property |

## 11. API surface

```
GET    /api/v1/system/health            aggregate health + degradation state
GET    /api/v1/system/metrics           current + windowed series
GET    /api/v1/system/info              version, config hash, device, model descriptors

GET    /api/v1/cameras                  list with health
GET    /api/v1/cameras/{id}
POST   /api/v1/cameras/{id}/enable | disable | reconnect
GET    /api/v1/cameras/{id}/stream.mjpeg?overlay=...&fps=...
GET    /api/v1/cameras/{id}/snapshot.jpg?annotated=bool
GET    /api/v1/cameras/{id}/modules     per-module availability + reason

GET    /api/v1/incidents                filter, keyset paginate
GET    /api/v1/incidents/{id}           includes explanation
GET    /api/v1/incidents/{id}/evidence  manifest; files served separately
POST   /api/v1/incidents/{id}/acknowledge | dismiss | annotate | escalate
GET    /api/v1/incidents/{id}/export    report bundle

GET    /api/v1/events/timeline          bucketed; incidents + health events
GET    /api/v1/events/candidates        rejection feed, with reasons

GET    /api/v1/zones/{camera_id}
PUT    /api/v1/zones/{camera_id}        validated; takes effect without restart

GET    /api/v1/analytics/summary        by type, camera, hour; dismissal rates

POST   /api/v1/mutes                    create a scoped, expiring mute
DELETE /api/v1/mutes/{id}

WS     /api/v1/telemetry                the union in §5
```

`PUT /zones` applying without a restart is a hard requirement: zone tuning is iterative and
a restart-per-edit loop makes it unusable, which means it will not get done, which means the
false-positive rate stays high.

## 12. Build and serving

Development: Vite dev server on 5173 proxying `/api` to the core on 8088, so the console
has hot reload while the pipeline runs undisturbed. `scripts/dev.ps1` starts both.

Production: `npm run build` emits `ui/dist`, which the API mounts as static files at `/`.
One process, one port, one URL, no separate web server to configure. The console is served
from the same origin as the API, so there is no CORS configuration to get wrong.
