# 0016 — Local accounts, roles and the desktop engine boundary

- **Status:** accepted (owner approved the desktop plan on 2026-11-02; see "What was and was not decided")
- **Date:** 2026-11-02
- **Phase:** D0
- **Supersedes:** 01 §2 and 10-roadmap §10, which listed multi-user auth and RBAC as "not planned"
- **Evidence:** `docs/DESKTOP-AUDIT-AND-PLAN.md` §4, §5, §7

## Context

01 §2 chose a single-operator console with an optional shared secret. Three things since then make
that insufficient for a desktop product:

1. Operator actions are recorded as the literal string `"operator"`
   (`response/operator_actions.py`), so the audit trail cannot say who acknowledged, dismissed or
   muted an incident.
2. With the bearer token enabled, a browser `<img>` cannot send headers, so the live stream cannot
   authenticate (LIMITATIONS, "API and security").
3. A desktop shell puts the engine behind a loopback port that **any local process or user can
   reach**. A hidden port is not an authenticated one.

## Decision

**Local, offline accounts, with the engine as the only authority.** No cloud identity provider and
no account that leaves the machine.

- **Layer.** A new package `vigil.auth` sits **below** `vigil.api` and is imported only by `api/`
  and `cli/`. The engine's decision packages (`temporal/`, `verification/`, `severity/`, `events/`,
  `response/`, …) never import it. `response/` receives an **actor as a parameter**; it never looks
  one up, which keeps the engines independent of the UI and of accounts (rule 1). An import-linter
  contract enforces this.
- **Passwords:** Argon2id (`argon2-cffi`), per-user salt, parameters stored with each hash, rehash on
  login when parameters are raised. Minimum length 12, maximum 128, no composition rules, rejected if
  on a bundled common-password list or containing the username. The list is checked locally; there is
  no network call.
- **Sessions:** server-side records; the cookie holds a 256-bit random id and the store keeps only its
  SHA-256. `HttpOnly`, `SameSite=Strict`. Idle timeout 15 min (configurable 5 min to 8 h), absolute
  lifetime 12 h. Logout and expiry delete the record. A *lock* flag on the session makes data and
  stream routes answer 423 while the engine keeps monitoring.
- **Lockout:** after 5 consecutive failures, 30 s, doubling to a 15 min cap, keyed by the lower-cased
  submitted username **whether or not the account exists**, so lockout state does not reveal which
  users exist. An unknown user still runs a dummy hash verification so the response time does not
  either.
- **Roles:** Administrator, Operator, Viewer, with the permission table of the plan §7.7. **Every
  route declares exactly one requirement through a security dependency** (`api/session.py::requires`),
  which appears in the OpenAPI document as a scope on the operation. A test reads that document and
  fails the build when an operation has none, has two, or is open without being named in
  `PUBLIC_ROUTES`; a second test proves the checker itself fails on a route with no requirement. The last Administrator cannot be deleted or demoted.
- **First launch:** no default account. The engine answers `setup_required` and accepts exactly one
  setup call, which must present a one-time **setup token** issued by the supervising shell (or
  printed once on the console when run headless). The token is consumed on first use. This closes the
  race in which another local process claims the first administrator.
- **Recovery without email:** ten one-time recovery codes stored as Argon2id hashes and shown once; an
  Administrator can reset other users; `vigil auth reset-admin` is the last resort. It needs access to
  the data directory, which is the same thing an attacker holding the disk has, so it asks for a typed
  confirmation phrase and does not pretend an elevation check would be a barrier. (The plan said
  "elevated prompt"; that was wrong, because the account database is owned by the user, not by the
  administrator.)
- **Audit:** an append-only table in its **own database file** (`auth.sqlite3` beside the incident
  database), each row carrying a SHA-256 chain over the previous row. This is tamper-**evident**, not
  tamper-proof.
- **Streams and downloads** authorise by session cookie. The bearer token remains for programmatic
  clients, is never accepted for human sign-in, and is unchanged in behaviour when accounts are not
  yet set up.
- **Origin discipline:** state-changing routes and WebSocket upgrades require a `Host` and `Origin` of
  the loopback origin the engine is serving, and a custom request header, in addition to
  `SameSite=Strict`.

## Consequences

- The audit trail can finally name a person. `incident_transitions.actor` and `mutes.actor` were
  already `TEXT NOT NULL`, so **no incident-database migration is needed**.
- The engine gains a second SQLite file. Incident retention, vacuum or restore cannot touch the audit
  log, and the audit log cannot be lost by deleting evidence.
- Every new route must declare a permission, which is a small, permanent tax on adding endpoints and
  the point of the test.
- **What this does not do.** It does not stop an administrator on the operating system, anyone holding
  the disk (nothing here is encrypted at rest; BitLocker is the control), or malware running as the
  logged-in user. Logs are chained, not sealed. The threat model is in the plan §7.9 and is not
  restated or softened here.
- A locally stored system cannot be both secure against someone with the disk and recoverable by
  someone who lost every secret. `vigil auth reset-admin` is the honest end of that trade.

## What was and was not decided

On 2026-11-02 the owner approved the plan as written. That approval is recorded here as accepting the
plan's recommendations on three of its seven open decisions, which this ADR depends on:

| # | Decision | Recorded outcome |
|---|---|---|
| 5 | Shell-issued setup token | **Accepted** as recommended |
| 6 | Reverse 01 §2 (accounts, roles, audit) | **Accepted** — this ADR |
| 7 | Locked console may notify severity and camera only, never an image | **Accepted** as recommended; no notification code exists yet |
| 1 | Project licence | **Not decided.** Nothing here chooses one |
| 2 | NVIDIA runtime policy | **Not decided** |
| 3 | Code-signing certificate | **Not decided** |
| 4 | Update hosting and key custody | **Not decided** |

Decisions 1 to 4 do not block D0 and are not assumed. 1 and 2 block the installer in D4; 3 and 4 block
signed updates.

## Rejected alternatives

- **Keep the shared bearer token and add nothing.** Cannot attribute actions and cannot authenticate a
  stream; rejected in the plan §7.
- **Tie accounts to Windows logins.** Would make the engine Windows-specific, and the aim is to gate the
  console and attribute actions, not to mirror OS accounts.
- **"Remember this device".** Persists a credential on a console meant to lock.
- **Enforce roles in the UI only.** A hidden button is not a permission.
- **Store sessions in a signed cookie.** Logout and revocation would not be server-side; the plan
  requires both.
- **Put audit rows in the incident database.** A retention sweep or vacuum could touch them.
