# D0 report: foundation

**Date:** 2026-11-02 · **Branch:** `feat/d0-foundation` · Nothing pushed or merged.

## What was built

| Item | Where | Verified by |
|---|---|---|
| ADR 0016: local accounts, roles, desktop boundary | `docs/adr/0016-*.md` | review |
| `vigil serve`: starts with no cameras, `--port 0`, JSON lines on stdout, logs on stderr, `--stop-on-stdin-eof`, `--ui-dir` | `cli/commands/serve.py` | `tests/unit/cli/test_serve.py` (real subprocess for the stdin shutdown) |
| Accounts layer: Argon2id, sessions, lockout, recovery codes, hash-chained audit, own SQLite file | `src/vigil/auth/` | 46 tests in `tests/unit/auth/`; import-linter contract `auth-is-self-contained` |
| Roles and request discipline on every route; Viewer refused on every mutating route | `api/session.py`, `api/routers/auth.py` | `tests/unit/api/test_accounts_api.py` (24 tests over real HTTP) |
| Route-permission test: reads OpenAPI, fails on a route with no requirement or two, and a second test proves the checker can fail | same | same |
| Actor threaded through operator actions; row mapper no longer coerces names to `system` | `response/operator_actions.py`, `persistence/mappers.py` | `tests/unit/response/test_lifecycle.py` |
| `vigil auth reset-admin`, `vigil openapi` | `cli/commands/` | `test_serve.py`, `test_openapi.py` |
| Frontend: `ui/` (React 19, Vite, typed client, tokens, contrast script) and `desktop/` (Electron, supervisor) | `ui/`, `desktop/` | below |
| Gates: size, paths, secrets over frontend source; `check.ps1` runs ui and desktop typecheck, lint, format, contrast, tests with coverage floors, build, generated-types-current, and the real-app end-to-end run | `scripts/` | `test_gates_and_architecture.py` |
| F7 fixed: a warming-up camera is no longer held to the driver's claimed rate | `ingest/health.py` | `test_health.py`, `test_reliability.py` |
| F8 fixed: stream pacing on a slot grid | `pipeline/streaming.py` | `test_stream_pacing.py` |

## Verification

- Engine suite: 2,005 passed, 2 skipped before the later additions; frontend: ui 17 tests (coverage 100% lines), desktop 35 unit tests.
- **Real app end to end** (Playwright launches Electron, which launches the engine): the console renders and says setup is required; axe at WCAG 2.2 AA is clean in Chromium **including colour contrast**; the page has no Node and a three-call bridge; the setup token is handed over once; a killed engine is restarted and the window says why; quitting leaves no engine process.
- Argon2id tuned: 64 MiB / 3 passes 100 ms, 64 MiB / 4 passes 130 ms, **128 MiB / 3 passes 209 ms** (p50 of 7, this machine); the last is the default.

## The Electron spike (plan §5.2), 4 synthetic cameras, null detector, this machine

| Budget | Result |
|---|---|
| Shell tree under 700 MB private | **188 MB private**, 371 MB working set, 4 processes |
| First frame within 8 s | **5.1 s** (engine ready at 2.2 s; the rest is first-run setup, about 2 s of which is hashing ten recovery codes) |
| Under 150 MB extra VRAM | **+31 MB** (a before/after difference while other applications hold about 4.5 GB, so noisy) |

All three met, so Electron stays and Tauri is not evaluated. **Not measured:** the spike with the real GPU detector (a run was started and stopped by the owner), so first-frame time with a model load is unknown. Reproduce with `node desktop/scripts/spike.mjs`.

## What was not verified

- **F7 and F8 on the real webcam.** Both are pinned by unit tests that reproduce the mechanism; no new live run was made after the fixes. F8's cause (a wait that rounds up to the Windows timer tick) is inferred from the measured rate and the code, not observed.
- `Secure` cookie on the loopback origin: the cookie is `HttpOnly` and `SameSite=Strict`; whether Chromium accepts `Secure` there was not tested.
- Engine start time with a real model; the unplug, disk-full and other drills.
- The bundled common-password list is a few hundred entries written for this project, not the 100 k list the plan names.

## Deviations from the plan

- Route permissions are declared with a security dependency that appears in the OpenAPI document, not a central table; ADR 0016 says so.
- `reset-admin` needs a typed phrase, not an elevated prompt: the account database is the user's own file, so elevation would be theatre.
- `@axe-core/playwright` cannot drive Electron (it opens a second page), so axe-core is injected into the real window.
- TypeScript is 5.9, not 7: typescript-eslint does not yet support 7. ESLint is 9.39 for the same reason with jsx-a11y; npm marks it unsupported, which is a known cost.

## Still open (owner decisions 1 to 4 from the plan)

Project licence, NVIDIA runtime policy, code-signing certificate, update hosting and key custody. None blocks D1.
