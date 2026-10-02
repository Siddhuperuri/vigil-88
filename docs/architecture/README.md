# VIGIL-88 — Architecture Documentation Set

> Status: **DESIGN** — no implementation exists yet. Nothing in this set describes
> working software. Everything here is a commitment about what will be built and how.

VIGIL-88 is a real-time computer-vision incident detection and situational-awareness
platform. Its defining property is that it **reasons across time**: a frame is evidence,
not a verdict. Incidents are produced by accumulating evidence over temporal windows,
verifying it against cross-cutting rules, and scoring it — never by classifying a single
frame and raising an alarm.

## Reading order

| # | Document | What it settles |
|---|----------|-----------------|
| 01 | [Architecture](01-architecture.md) | System context, runtime topology, layering, the nine engines, cross-cutting concerns, decision register |
| 02 | [Directory Structure](02-directory-structure.md) | Every directory, what belongs in it, dependency rules, enforcement |
| 03 | [Dependency Strategy](03-dependency-strategy.md) | Package manager, pinning, dependency groups, GPU/CPU install matrix, what we refuse to depend on |
| 04 | [AI Pipeline Design](04-ai-pipeline.md) | The nine engine interfaces, batching, label normalization, temporal math, degradation ladder |
| 05 | [Data Model](05-data-model.md) | Domain types, persistence schema, identifiers, evidence layout, retention |
| 06 | [Event Model](06-event-model.md) | Observation → CandidateEvent → Incident, plugin contract, verification, severity, lifecycle state machine |
| 07 | [Camera Pipeline Design](07-camera-pipeline.md) | Capture threading, buffering, backpressure, reconnection, health state machine, clock discipline |
| 08 | [UI Architecture](08-ui-architecture.md) | Console structure, transport, state ownership, visual identity, design tokens |
| 09 | [Testing Strategy](09-testing-strategy.md) | Test tiers, synthetic scene generation, determinism, contract tests, quality gates |
| 10 | [Development Roadmap](10-roadmap.md) | Phases P0–P6, exit criteria, what is deliberately deferred |

## Target hardware (measured on this machine, 2026-10-01)

| Property | Value |
|----------|-------|
| GPU | NVIDIA GeForce RTX 3050 **6 GB Laptop** GPU |
| VRAM | 6144 MiB |
| Driver | 616.56 |
| CUDA toolkit | Not installed (not required — PyTorch wheels bundle the CUDA runtime) |
| Python | 3.11.9 (`py -V:3.11`) |
| Node | 20.20.2 / npm 10.8.2 |
| Package manager | uv 0.12.3 |
| OS | Windows 11 Pro 26300 |

**The GPU is a Laptop (mobile) part.** Its sustained throughput is materially lower than a
desktop RTX 3050 because it is power- and thermal-limited, and it clocks down under load.
Every performance figure in this set is therefore a *budget to be measured*, never a claim.
See [10-roadmap.md](10-roadmap.md) for the measurement gate.

## Three rules that outrank convenience

1. **No fake functionality.** A control that cannot do its job does not ship. A feature that
   cannot be implemented yet ships as a documented interface plus an entry in
   `docs/LIMITATIONS.md` — and the UI renders it as explicitly unavailable, not as a button
   that silently does nothing.
2. **No performance claims without measurement.** The words "real-time" do not appear in
   user-facing text, READMEs, or commit messages until `vigil bench` has produced a
   committed `docs/PERFORMANCE.md` on this hardware.
3. **No wall-clock reads inside decision logic.** All temporal reasoning uses timestamps
   carried on the frame, obtained from an injected `Clock`. This is what makes recorded
   video replay behave identically to live capture, and what makes the temporal engine
   testable. Violations are a review blocker.

## Document conventions

- **MUST / MUST NOT / SHOULD** are normative. Everything else is explanation.
- Every tunable value named in these documents maps to a config key, written as
  `section.key_with_units`. Units are part of the name (`_ms`, `_px`, `_ratio`, `_mb`,
  `_fps`). A number with no config key is a defect.
- `D-00n` references point to the decision register in [01-architecture.md](01-architecture.md).
- Default parameter values given here are **initial defaults for tuning**, not tuned values.
