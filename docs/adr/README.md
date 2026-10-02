# Architecture Decision Records

Decisions currently live as the register in
[../architecture/01-architecture.md](../architecture/01-architecture.md) §11, as `D-001`
through `D-010`. That is deliberate while the whole design fits in one reviewable set.

A decision is split out into its own file here when it needs more than a paragraph of
context — typically because it was reversed, because measurement contradicted the original
reasoning, or because the trade-off needs recording at length.

## Rules

- A decision is never edited to say something different. It is superseded by a new,
  higher-numbered record that states what changed and why.
- New decisions taken during implementation start at `0011`, continuing the `D-0nn`
  numbering so references stay stable across both locations.
- Filename: `NNNN-short-kebab-title.md`.

## Template

```markdown
# NNNN — Title

- **Status:** proposed | accepted | superseded by NNNN
- **Date:** YYYY-MM-DD
- **Phase:** P0..P6

## Context
What forced a decision. Include the measurement, if there was one.

## Decision
What we are doing.

## Consequences
What this costs, and what it makes harder. Honest, not promotional.

## Rejected alternatives
Each with the specific reason it lost.
```
