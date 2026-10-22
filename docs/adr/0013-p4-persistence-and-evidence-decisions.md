# 0013 — P4: stdlib SQLite, and how evidence timing works

- **Status:** accepted
- **Date:** 2026-10-18
- **Phase:** P4 (incidents, persistence, evidence)

Three decisions taken while implementing P4 that depart from, or add to, the design set.
Recorded here rather than silently absorbed, per the working agreement in 10-roadmap §11.

## A. Stdlib `sqlite3` instead of SQLAlchemy and Alembic

`03-dependency-strategy` §3.1 lists `sqlalchemy >=2.0` and `alembic >=1.13` as core
dependencies, and `02-directory-structure` §2 shows `persistence/orm.py` and
`persistence/migrations/` (alembic). **Neither is installed.** `persistence/` uses the standard
library's `sqlite3` with a hand-written migration runner.

### Why

1. **The same document prefers the stdlib.** §1 rule 6 names `sqlite3` among the modules that
   "cover a surprising amount of this system", and rule 1 says every dependency is a liability.
   §3.1's justification for SQLAlchemy is "plain `sqlite3` would mean hand-rolling migrations and
   query composition" — which is true, and turned out to cost about 200 lines, both tested.
2. **The write discipline of 05 §5 *is* the design.** One serialised writer, a read-only
   connection per thread, explicit PRAGMAs (`WAL`, `synchronous=FULL`, `busy_timeout`,
   `foreign_keys`), and one transaction per incident including its transitions. Those are
   properties of specific connections used in specific ways. A connection pool between
   `database.py` and those guarantees makes them harder to verify, not easier.
3. **Analytics (P5) is aggregate SQL.** 03 §8 already rejected pandas on the grounds that
   "`sqlite3` aggregate SQL" is the right tool. A query builder would sit between that SQL and
   the person reading it.
4. **No new supply-chain surface**, and the tests for `domain/`, `temporal/`, `verification/`
   and `severity/` keep running in the bare environment §1 rule 4 requires.

### Cost, and what pays it

- A migration runner (`persistence/migrations.py`): forward-only, one transaction per migration,
  never destructive, refuses a database written by a newer build rather than guessing at columns
  it does not know. `schema_version` records every applied version with its name and timestamp.
- Hand-written row mapping (`persistence/mappers.py`), which 02 §4 called for anyway — it
  specifies `mappers.py` as the only module that knows both a domain type and a row.
- No `Mapped[...]` type checking over the schema. Mitigated by `mypy --strict` over the mappers
  and by the repositories being the only code that writes SQL.
- **SQL is assembled by string formatting in two layers**, which `ruff`'s S608 flags. The
  interpolated text is always a column tuple declared in the same module or a run of `?`
  placeholders; a caller's value is only ever bound. S608 is switched off for
  `persistence/` and `analytics/` in `pyproject.toml`, and
  `tests/unit/persistence/test_query_safety.py` enforces the real rule instead: hostile
  identifiers and filter values are fed through the builders and asserted never to reach
  statement text, sort keys outside a fixed allow-list are refused, LIKE wildcards inside a
  value are escaped, and the same payloads are run against the real schema.

### What is unchanged

D-006 still holds: all access sits behind repository interfaces, so Postgres or Timescale is a
later swap rather than a rewrite. Nothing above L3 contains SQL.

*Reversing this* means adding the two dependencies and reimplementing `database.py`,
`migrations.py` and `mappers.py` against them. The schema is plain SQL in `migrations.py`, so an
alembic baseline could be generated from it.

## B. Evidence is written when the post-roll window closes, not at confirmation

05 §6 lists `clip_post.mp4` as "from confirmation forward", which cannot be written at
confirmation because the footage does not exist yet. Writing the incident later instead would
mean a confirmed incident that is briefly absent from the database — unacceptable.

So the two are separated, which 06 §9's sequence already allows (CONFIRMED, *then* "evidence
complete"):

- the incident is persisted immediately with `evidence_status = pending`;
- a capture is held open until `confirmed + evidence.postroll_seconds`;
- **snapshots are taken at confirmation** and held as bytes, because by completion time the
  latest frame shows an empty scene;
- **clips are sliced at completion** from the camera's ring buffer;
- the status then becomes `complete` or `partial`, and `partial` names what is missing.

Consequences, all deliberate:

- `EvidenceStatus` gains `PENDING`, `PARTIAL`, `UNAVAILABLE` and `DISABLED`. The console shows
  these rather than an empty evidence list.
- **One ring buffer spans pre-roll *and* post-roll** (`PreRollBuffer`, window
  `preroll_seconds + postroll_seconds`), so the byte ceiling `evidence.preroll_max_mb` is the
  bound that actually binds. Q3's encoded-JPEG decision is what makes this affordable.
- Open captures are bounded by `evidence.max_concurrent_captures`; beyond it the oldest
  completes early with a shorter post-roll, which is recorded in its manifest. Degradation, not
  loss.
- At shutdown every open capture is written with whatever exists, and says in its manifest that
  it is shorter than configured.

## C. Additions to the design set

| Addition | Why |
|----------|-----|
| A **pre-roll sampler worker**, separate from the response worker | The response worker spends hundreds of milliseconds muxing a clip; a hole in the pre-roll for the duration of every capture would be a hole in exactly the footage someone wants. 01 §4 lists only `ResponseWorker`. |
| `AnalysisTick.ongoing` and `storage.incident_extend_interval_ms` | 06 §9 requires a live incident's `last_observed` to follow the evidence, but promotion happens once per episode — an active accumulator is never promoted again, which is what makes one intrusion one incident. The fact that evidence is *still arriving* therefore needs a route to the response stage other than a new candidate. Throttled, because the only thing it changes is a timestamp. |
| `domain/mute.py` | `Mute` was defined in `verification/validators/context.py`, and `response/` creates mutes while `verification/` honours them. Two L4 siblings must not import each other (01 §5), so the shared value object moved to L2. `lint-imports` caught this. |
| `config/schema/taxonomy.py` | `config` (L1) cannot import `domain` (L2), but config validation must still reject an unknown event type at startup. The names are mirrored and `tests/unit/config/test_taxonomy.py` proves the mirror exact. This also **fixes a pre-existing `lint-imports` failure**: `config/schema/severity.py` had been importing `domain.enums` at runtime, and the `layers` contract had been red on `master`. |
| `pipeline/stages.py`, `response_stack.py`, `camera_runtime.py`, `shutdown.py` | `app.py` was at the 500-line gate. Composition moved out; the application object keeps lifecycle. |
| A clamp on terminal timestamps | `IncidentManager.transition` never writes a `resolved_utc` earlier than `confirmed_utc`. The `Incident` constructor refuses that pair, so such a row would be *unreadable* — the incident would vanish from the console rather than look odd. A wall clock can legitimately read backwards between two writes (an NTP correction). |

## D. Corrections to the design documents

- **05 §5's `candidate_events` table lacks the accumulator trace**, which 05 §4's
  `CandidateRecord` carries and which is the whole point of persisting a rejection ("how
  confidence evolved"). The implemented table adds `trace_json`, plus `support_ratio`,
  `observations` and `incident_id` so the rejection feed can be rendered and a verified
  candidate can name the incident it became.
- **05 §5's `incidents` table lacks a dedup key.** Without one, "is there an open incident for
  this situation" is a scan of the whole table on the response path. The implemented table adds
  `dedup_key` with an index on `(dedup_key, status)`, plus `candidate_id`, `evidence_status`,
  `acknowledged_utc`, `dismissed_utc` and `updated_utc`.
- **A `mutes` table is required and 05 §5 does not list one.** 06 §10 requires mutes to be
  visible with a countdown and to survive a restart, which a monotonic expiry cannot do.
