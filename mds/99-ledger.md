# Decision ledger

Append-only. Never delete an entry; supersede it with a new one that references the old id. An `ADR` is reversed only by another `ADR`.

- `ADR-NNN` a decision: context, options, choice, consequences.
- `LEARN-NNN` something discovered the hard way, with evidence.
- `RISK-NNN` an accepted risk, with its trigger and mitigation.

---

## ADR-001 — Guarded conditional UPDATE as the atomic decision

**Date** 2026-10-03

**Context** Seat allocation must produce exactly one winner per seat under thousands of concurrent claims, with losers receiving a clean 409.

**Options**
1. `SELECT` then `UPDATE` in a transaction — double-sells at `READ COMMITTED` via lost update.
2. Guarded conditional `UPDATE` whose `WHERE` clause carries the state precondition.
3. `SERIALIZABLE` isolation with application retries.
4. Advisory locks keyed on seat labels.
5. Insert-only claim table relying on a unique constraint.

**Choice** Option 2, with option 5's unique index retained as a backstop.

**Reasoning** The returned row count *is* the decision, so no window exists between check and effect. Under `READ COMMITTED`, PostgreSQL re-evaluates the `WHERE` clause after a blocking row lock is released, so a loser's predicate fails and it affects zero rows. Option 3 is correct but converts contention into serialization failures requiring retries — a retry storm at 20k concurrency. Option 4 adds a second locking system to reason about with no benefit over the row locks that already exist.

**Consequences** Correctness depends on documented `READ COMMITTED` re-evaluation semantics, mitigated by the independent backstop index. Isolation level is fixed at `READ COMMITTED`. A hot seat produces a lock queue, bounded by `lock_timeout`.

---

## ADR-002 — All-or-nothing multi-seat reservations

**Date** 2026-10-03

**Context** A request for several seats where only some are free needs defined behaviour that holds under concurrency.

**Options** All-or-nothing versus best-effort partial.

**Choice** All-or-nothing. Any unavailable seat declines the whole request with 409 and the conflicting labels; nothing is claimed.

**Reasoning** One reservation is one atomic unit, so `amount_paise` and idempotent replay are exact with no subset semantics. It is enforced by transaction rollback rather than by compensating releases, so there is no cleanup path to get wrong. For assigned seating it is also the honest product semantic — nobody wants one of two adjacent tickets.

**Consequences** Lower conversion under contention. A client wanting best-effort must issue separate requests with separate keys, which is strictly more explicit.

---

## ADR-003 — Hold with TTL, then explicit confirm

**Date** 2026-10-03

**Context** The requirements permit either an explicit cancel or a time-boxed auto-expiring hold, and require `GET /shows/{id}` to report available, held, and confirmed.

**Choice** Reserve returns `held` with `expires_at`. `POST /reservations/{id}/confirm` promotes to `confirmed`. `cancel` releases early. Expiry is enforced lazily in the claim predicate and eventually by a sweeper.

**Reasoning** All three seat states genuinely exist and are observable, which the state contract demands. A hold is also where a payment step would attach without reworking the claim. Both release models are implemented, which is strictly more than either alone.

**Consequences** Reserve returns `status: "held"`, not `"confirmed"`. Deliberate, and documented in the README, since the requirements leave the hold model open and demand all three seat states be observable. Confirm must be reachable before a token expires, hence the guest-token-lifetime validation.

---

## ADR-004 — Blocking FOR UPDATE with a lock timeout, not NOWAIT

**Date** 2026-10-03

**Context** 500 contenders on one seat form a 500-deep lock queue.

**Options** Block on the row lock with a timeout, or `NOWAIT` / `SKIP LOCKED` to decline immediately on contention.

**Choice** Block, bounded by `lock_timeout`; a timeout translates to 409 `SEAT_TAKEN` with its own metric label.

**Reasoning** `NOWAIT` declines seats that were merely momentarily locked by a transaction that then rolled back — spurious declines, and in a multi-seat all-or-nothing world a transaction that rolls back after locking its first seat would cause them routinely. Critical sections are sub-millisecond index updates, so the queue drains fast.

**Consequences** Tail latency on a hot seat grows with queue depth, observable via `seat_claim_lock_wait_seconds` and bounded by `lock_timeout`. A lock-timeout storm is visible rather than silent.

---

## ADR-005 — Per-user limit via quota-row lock with a derived count

**Date** 2026-10-03

**Context** A count-then-claim loses the race exactly as a double-sell does. Ten parallel requests against a limit of four must end with four.

**Options**
1. Count then claim — races.
2. Stored counter on a quota row, incremented and decremented.
3. Lock a quota row, then count from `seats`.
4. A database constraint expressing the limit — not expressible in PostgreSQL without a trigger or an exclusion constraint over aggregates.

**Choice** Option 3.

**Reasoning** The row lock serializes one principal's concurrent attempts, so the count is never stale when acted on. Deriving the count from `seats` is self-healing: a stored counter must be decremented on cancel, on sweep, and on every future release path, and a drift either blocks a legitimate booking forever or silently raises the limit.

**Consequences** One extra index-only scan per reserve. Different principals never contend, so no cross-user serialization is introduced. An invariant is created: no transaction may acquire a quota lock *after* a seat lock, or the deadlock-freedom argument breaks.

---

## ADR-006 — Keeping the quota lock rather than collapsing a round trip

**Date** 2026-10-03

**Context** The limit check and the claim could be collapsed into one statement with a `NOT EXISTS` guard, removing a round trip and the quota lock.

**Choice** Keep the quota lock.

**Reasoning** The lock is not only about the count — it is what serializes a single principal's concurrent requests. Without it the limit is checkable but not enforceable, because two of that principal's requests could evaluate the same guard concurrently.

**Consequences** One round trip and one lock acquisition per reserve, paid knowingly. Correctness over latency.

---

## ADR-007 — Per-instance rate limiting, no Redis

**Date** 2026-10-03

**Context** Rate limiting is required, and an on-sale burst sends on the order of 20,000 requests in roughly one second.

**Choice** In-process token bucket keyed by principal where a token exists and by IP only for pre-auth routes, with per-route-class ceilings as environment variables and a global disable flag.

**Reasoning** Keying on principal rather than IP is the decision that matters: 20,000 distinct buyers behind one load generator share an IP but are distinct principals, and an IP-keyed limiter would throttle the exact burst the service exists to handle. Redis would make the ceiling globally exact at the cost of a new dependency, a new failure mode, and a network round trip on the hot path — precision no correctness property needs.

**Consequences** With N instances the effective ceiling is N × configured. See RISK-003. The token-bucket interface is the seam if an exact global quota is ever required.

---

## ADR-008 — Audit via a bounded queue and a batched writer

**Date** 2026-10-03

**Context** Every request must be recorded without degrading throughput.

**Options** Synchronous insert in the request transaction; async bounded queue with a batched writer; logs only.

**Choice** Bounded `asyncio.Queue`, `put_nowait` on the request path, drained in batches by a worker on a dedicated connection.

**Reasoning** A synchronous insert doubles write amplification on the hot path during peak contention. Awaiting queue capacity would make audit a source of backpressure on bookings, inverting the priority: losing an audit row is an inconvenience, failing a booking is a defect.

**Consequences** Audit records can be dropped under saturation. Drops are counted and logged at `warning`, never silent. The queue is flushed on shutdown with a bounded timeout. `audit_log` carries no foreign keys, so a per-row FK check never touches the hot path.

---

## ADR-009 — Guests as real user rows, upgradeable

**Date** 2026-10-03

**Context** Both authenticated and guest buyers must be supported, and identity must be token-derived.

**Choice** `POST /auth/guest` creates a `users` row with `is_guest=true` and issues a short-lived token. `POST /auth/upgrade` attaches credentials to the same `user_id`.

**Reasoning** A guest being a real row means every authorization check, limit, and ownership rule applies unchanged — there is no guest-specific code path to get wrong. Preserving `user_id` on upgrade means reservations survive signup with no data movement. `ck_users_creds` makes a half-upgraded row unrepresentable.

**Consequences** Guest rows accumulate and need eventual garbage collection. `/auth/guest` needs an IP-keyed limit, since there is no principal yet. Guest token lifetime must exceed hold TTL, validated at startup.

---

## ADR-010 — Idempotency key committed before the effect, completed with it

**Date** 2026-10-03

**Context** The same key must reserve exactly once, including when two requests carrying it overlap.

**Choice** Two transactions. T1 inserts the key `in_progress` and commits. T2 performs the reservation and marks the key `completed` with the stored response, atomically. A concurrent duplicate detects the unique violation, then polls for a bounded interval and replays, or declines 409 `IDEMPOTENCY_IN_PROGRESS`.

**Reasoning** Ownership must be visible immediately, so the `in_progress` insert cannot be inside T2 — it would be invisible during exactly the window it exists for. Completion must be inside T2, or a crash between them leaves a committed reservation whose key says `in_progress` forever.

**Consequences** Two transactions per reserve. A crashed worker leaves a stale `in_progress` row, handled by a staleness-window reclaim. Domain declines are stored and replayed; an unexpected 5xx releases the key so a genuine retry can re-attempt.

---

## ADR-011 — PostgreSQL in development and production, no SQLite

**Date** 2026-10-03

**Context** Neither Postgres nor Docker is installed on the development machine, making SQLite locally superficially attractive.

**Choice** PostgreSQL 16 everywhere, via Homebrew or compose locally.

**Reasoning** SQLite cannot express `SELECT … FOR UPDATE`, has different predicate re-evaluation semantics, and does not support partial unique indexes the same way. The single thing that must be proven correct is precisely the part that would differ between engines, so it would be untested until it reached production.

**Consequences** A local Postgres is a prerequisite for Stage 1.

---

## ADR-012 — The effective-status expression is defined exactly once

**Date** 2026-10-03

**Context** A hold that has lapsed but has not been swept is claimable, and so must report as `available`. The claim predicate, the sweeper, and the counts query all need that notion.

**Choice** One SQL expression in `db/sql.py`, imported by all three.

**Reasoning** Three copies that drift is the single most likely cause of an apparent reconciliation failure during a burst — the API would report `held` for a seat the next claim hands out.

**Consequences** A change to expiry semantics is a one-line change in one place. Re-typing the expression anywhere is a review-blocking defect.

---

## RISK-001 — Free-tier cold start and database expiry

**Trigger** Render free web services spin down after idle; free managed databases expire after a fixed window.

**Impact** The first request after idle takes seconds. An expired database makes the live URL return 503 — a dead service that looks like a code fault.

**Mitigation** `/readyz` as the platform's health gate; `DB_POOL_MIN` pre-warmed; burst phase 0 warms before measuring; the cold start documented in the README so it is not read as a fault; a calendar check on database expiry with a documented recreation procedure.

## RISK-002 — Role carried in the token

**Trigger** A role change does not take effect until the access token expires.

**Impact** A demoted admin retains admin rights for up to the access token lifetime.

**Mitigation** Short access token lifetime. Accepted to avoid a user lookup per request. A `jti` denylist is the fix if it ever matters.

## RISK-003 — Rate limit ceilings multiply by instance count

**Trigger** More than one instance.

**Impact** Effective ceiling is N × configured.

**Mitigation** Accepted — the limiter is abuse protection and no correctness property depends on it. The token-bucket interface is the swap seam. See ADR-007.

## RISK-004 — Gauges lag by up to one refresh interval

**Trigger** A burst in flight while gauges are refreshed on an interval.

**Impact** An observer comparing a gauge to `GET /shows/{id}` mid-burst sees up to one interval of difference.

**Mitigation** Short configured interval; gauges derived from the same SQL as the API so they cannot disagree about semantics; counters are exact and inline; the reconciliation alert requires the mismatch to persist beyond one interval. `GET /shows/{id}` is always exact.

## RISK-005 — Migrations run from the container entrypoint

**Trigger** A rolling deploy with more than one instance, or a long migration.

**Impact** Concurrent migration attempts; a long migration delays startup.

**Mitigation** Single instance currently. Alembic's version table makes the loser a no-op. Schema changes are additive within a release so a rollback stays safe. A separate pre-deploy release phase is the upgrade path.

---

## LEARN-001 — Development machine baseline

**Date** 2026-10-03

System Python is 3.9.6, too old; Homebrew Python 3.14.3 is present. Neither Docker nor PostgreSQL is installed — `docker`, `psql`, `postgres`, and `pg_ctl` are all absent. The repository had no commits at the start of work.

**Implication** Stage 1 cannot close without installing Postgres (`brew install postgresql@16`) or Docker. Without Docker locally, the image is only ever exercised by CI and the platform build, which is a weaker check than the clean-checkout requirement deserves.

**Resolved** 2026-10-03. PostgreSQL 16.15 and Python 3.13.16 installed via Homebrew; Colima provides a Docker daemon without Docker Desktop. See the prerequisites table in [14-stage-plan.md](14-stage-plan.md) for the resulting local setup.

---

## LEARN-002 — ADR-001's Postgres semantics verified empirically

**Date** 2026-10-03

The guarded conditional `UPDATE` was tested directly against PostgreSQL 16.15 before any application code existed, because the entire correctness argument rests on behaviour that was otherwise only asserted. Four probes against a single-row table using the exact claim predicate:

| Probe | Setup | Result |
|---|---|---|
| Blocking re-evaluation | Txn A claims and holds the row lock 2s, then commits; txn B issues the identical guarded `UPDATE` 0.4s in | B blocked, then reported **`UPDATE 0`**. Row remained A's. |
| 50-way contention | 50 concurrent claimers on one seat, no coordination | **1 × `UPDATE 1`, 49 × `UPDATE 0`, 0 errors.** Exactly one winner. |
| Rollback releases | Txn A claims, holds 1.5s, then **rolls back**; B claims 0.4s in | B blocked, then reported **`UPDATE 1`** and owns the seat |
| Lazy expiry | Row set to `held` with `hold_expires_at` in the past, no sweeper running | Claim returned **`UPDATE 1`** — a lapsed hold is claimable with no worker involved |
| Live hold protected | Row held by another principal with a future expiry | Claim returned **`UPDATE 0`** |

**Implication** ADR-001 is confirmed rather than assumed: `READ COMMITTED` re-evaluates the `WHERE` clause after a blocking row lock is released, a rolled-back claim correctly leaves the seat winnable, and the lazy-expiry arm of the predicate works independently of the sweeper. Zero errors under 50-way contention means losers are a *decision* (zero rows affected), not an exception to translate — which is what makes the "no 5xx for a domain outcome" requirement achievable rather than aspirational.

These probes are the specification for the Stage 4 concurrency tests and should be reproduced as automated tests rather than left as a one-off manual result.

## LEARN-003 — Local Postgres connection ceiling is 100

**Date** 2026-10-03

The Homebrew PostgreSQL 16 default is `max_connections = 100`. Per the pool arithmetic in [11-scalability.md](11-scalability.md), the pool must be sized against this, not against expected request concurrency — and locally the audit writer's dedicated connection plus test-suite connections come out of the same 100.

**Implication** Development pool defaults must stay well under 100 or the concurrency suite will exhaust the server and produce connection errors that look like application defects. The managed production ceiling will differ and must be read from the platform rather than assumed equal.

## LEARN-004 — zsh does not word-split unquoted variables

**Date** 2026-10-03

A shell helper holding connection flags (`DB="-h localhost -U seatres …"`) expanded as a *single* argument under zsh, so `psql $DB` passed the whole string to `-h`. zsh, unlike bash, performs no word splitting on unquoted parameter expansion.

**Implication** Any shell script in this repository — `burst.sh`, the container entrypoint, CI helpers — must not rely on bash word-splitting of flag-bearing variables. Use explicit arrays, `PG*` environment variables, or a `#!/usr/bin/env bash` shebang. The burst script is the one that matters: a silently misparsed base URL would make it measure the wrong target.

## LEARN-005 — Barrier-synchronised concurrency tests deadlock on pool capacity

**Date** 2026-10-03

The first asyncpg contention probe hung indefinitely with no output. Cause: each of 50 tasks acquired a pooled connection **and then** waited on an `asyncio.Barrier(50)`, against a pool capped at 30. Thirty tasks held connections while waiting for a barrier that needed fifty; the remaining twenty blocked forever in `acquire()` on connections that would never be released. A resource-then-barrier deadlock, entirely in the test harness — the service under test was not involved.

**Fix** Acquire every connection *before* the barrier, and size the pool above the participant count:

```python
conns = await asyncio.gather(*(pool.acquire() for _ in range(N)))   # all resources first
barrier = asyncio.Barrier(N)                                        # then synchronise
```

**Implication for the test suite** This is a trap the concurrency tests in [12-testing-and-burst.md](12-testing-and-burst.md) will hit, because barrier synchronisation is exactly how they force genuine overlap. Three rules follow:

1. Any barrier-synchronised test must acquire all scarce resources before the barrier, never inside it.
2. Test pool size must exceed the participant count, and the participant count must stay under the server's `max_connections` (100 locally, LEARN-003) with headroom for the suite's other connections.
3. A concurrency test that hangs is more likely a harness deadlock than a service defect. Check the harness before suspecting the claim path — and give every such test a hard timeout so a hang fails loudly instead of stalling a run.

**Verified result after the fix** 50 barrier-synchronised asyncpg claimers on one seat: 1 won, 49 declined, 0 errors, exactly one held row. The mechanism holds through the real driver, not only through `psql`.

## LEARN-006 — Stack versions validated together on Python 3.13

**Date** 2026-10-03

Resolved and imported cleanly in one environment: FastAPI 0.115.6, Pydantic 2.10.5, asyncpg 0.30.0, PyJWT 2.10.1, Alembic 1.14.1, SQLAlchemy 2.0.37, argon2-cffi, prometheus-client, on CPython 3.13.16. Argon2 hashing confirmed working, and asyncpg's C extension built without issue on arm64.

SQLAlchemy is present **only** so Alembic can run migrations. Runtime queries use raw asyncpg, per [09-repositories.md](09-repositories.md). No ORM model layer is to be introduced — an ORM would obscure the exact SQL the correctness argument depends on.

**Implication** Versions are pinned exactly in `pyproject.toml` and the container base must be `python:3.13-slim` to match. A floating dependency is a build that works today and fails from a clean checkout later.

---

## ADR-013 — No `events` table; `event_kind` lives on `shows`

**Date** 2026-10-03

**Context** The original schema had `events` as a parent aggregate with `shows.event_id` as a nullable FK. Reviewing the ticket breakdown against the requirements showed that no requirement reads an event: REQ-010 creates a show with seats and a price, REQ-012 reads a show, and the nullable FK meant shows already had to work standalone.

**Choice** Drop `events`. Carry `event_kind` as a validated `TEXT` column on `shows`.

**Reasoning** A table nothing reads, reached through a nullable FK, is cost without benefit: an extra migration, an extra join, and an ambiguity about whether a show has a parent. The generic-across-verticals property that motivated it is preserved — cinema and concert remain configuration, not code paths — because that property was always carried by `event_kind` and never by the table.

**Consequences** A future requirement for one event owning several shows (a festival, a film run) needs a migration to introduce the parent. Accepted: that migration is small and additive, and paying for it now buys nothing today.

## ADR-014 — `seats` stores only `label` and `section`

**Date** 2026-10-03

**Context** `seats` carried nullable `section`, `row_label` and `seat_number` alongside `label`, which is the canonical identity. No requirement reads any of the three.

**Choice** Keep `section`. Drop `row_label` and `seat_number`.

**Reasoning** `section` earns its place by grouping seats for tiered pricing, which the API already exposes through `seat_overrides`. `row_label` and `seat_number` are derivable from `label` by a helper, so storing them duplicates state that can disagree with the label it was derived from — on the hottest table in the system, where every column is paid for on every claim.

**Consequences** A seat-map response would need a label-parsing helper rather than three columns. Cheaper than keeping two unread columns coherent, and the parse has one source of truth.

## ADR-015 — `GET /shows` deferred

**Date** 2026-10-03

**Context** REQ-014, a paginated show list, was priority `should`. It requires keyset pagination, an opaque cursor codec and per-show counts.

**Choice** Defer it past Stage 7, and downgrade REQ-014 to `could`.

**Reasoning** Nothing depends on it. The burst script creates its own show and addresses it by id, and `GET /shows/{id}` (REQ-012, a `must`) carries every state read the invariant checks need. Deferring removes `helpers/pagination.py`, the cursor codec and their tests from the critical path.

**Consequences** No catalogue browse endpoint until it is built. The shape is documented in [06-apis.md](06-apis.md) so adding it later is mechanical.

## ADR-016 — REQ-048 amended: zero 5xx on domain paths, 503 only on genuine unavailability

**Date** 2026-10-03

**Context** Decomposing the requirements surfaced a direct contradiction between two committed documents. REQ-048 demanded "zero 5xx responses" across a sustained burst, while [11-scalability.md](11-scalability.md) named 503 `DATABASE_UNAVAILABLE` on a pool-acquire timeout as "the single legitimate 5xx in the service". As written, a correct implementation could fail REQ-048 under pool pressure, and the requirement was unsatisfiable rather than merely demanding.

**Options**
1. Forbid 503 entirely — would mean queueing indefinitely or lying about readiness when the database is gone.
2. Permit 503 freely — would gut the requirement, since any 5xx could be excused as pool pressure.
3. Permit 503 only for genuine unavailability, and require zero occurrences during a burst.

**Choice** Option 3. REQ-048 now reads: zero 5xx on every domain path; 503 `DATABASE_UNAVAILABLE` only when the database is genuinely unreachable; zero occurrences of it required during a burst.

**Reasoning** This keeps the requirement falsifiable while leaving the honest failure mode available. It also makes explicit something that was previously implicit: **pool sizing is part of this requirement**, not a tuning detail, because an undersized pool converts load into 503s that the requirement now forbids.

**Consequences** `unhandled_exceptions_total` must stay at zero through a burst and is the direct measurement, so it is added to REQ-042's required metric list alongside `db_pool_waiting` and `seat_claim_lock_wait_seconds`. The burst script reports 503s in their own column, separate from 4xx declines.

**Found by** the requirements decomposition, before any code existed — which is the decomposition paying for itself.

---

## ADR-017 — Reserve confirms by default; holds are opt-in; expiry is lazy only

**Date** 2026-10-03 · **Supersedes** ADR-003 · **Amends** ADR-012

**Context** ADR-003 made every reserve return `held` with a TTL, promoted by an explicit `confirm`, with expiry enforced lazily in the claim predicate *and* eventually by a background sweeper. Two problems emerged from thinking the load path through.

First, the exactly-one-winner property is measured per contested seat over a burst. A hold that lapses mid-burst is legitimately re-claimable, so a hot seat can correctly produce a *second* 201 within one run. Correct behaviour, but it makes the headline property unmeasurable — the burst cannot distinguish "two winners because a hold lapsed" from "two winners because the claim is broken".

Second, the sweeper's only job was making stored state match reality. Effective status is already derived on read, so the sweeper was redundant machinery on the correctness-adjacent path: a second writer of seat state, with its own lock story, its own failure mode, and nothing that depended on it.

**Options**

1. Keep hold-by-default plus the sweeper (ADR-003).
2. Keep hold-by-default, drop the sweeper.
3. Confirm by default, hold opt-in, drop the sweeper.
4. Confirm only — delete the hold capability.

**Choice** Option 3.

- `POST /shows/{id}/reserve` with no `hold_ttl_seconds` → **201, `status: "confirmed"`**. Seats become `confirmed`, `hold_expires_at` is `NULL`, the reservation is `confirmed` with `confirmed_at` set. This is the default and the path load exercises.
- With `hold_ttl_seconds` (clamped to the show maximum) → 201, `status: "held"`, `expires_at` set. `POST /reservations/{id}/confirm` promotes it; `cancel` releases it.
- Expiry is **lazy only**. The `(status='held' AND hold_expires_at <= now())` arm of the claim predicate is the entire expiry mechanism. `workers/hold_sweeper.py` is deleted from the design.
- Stored status is never the authority for a lapsed hold. An **effective status** expression is derived on read for seats *and* for reservations, defined once in `db/sql.py` and used by every reader — `GET /shows/{id}` counts, the per-user limit count, `GET /reservations`, and the confirm/cancel guards. This extends ADR-012 from one expression to two, with the same rule: one definition, cited everywhere, never retyped.

**Reasoning** Making `confirmed` the default removes lapsed-hold re-claims from the path load exercises, so exactly-one-201-per-hot-seat becomes a measurable property again, while the hold capability — and with it the payment-attachment point and all three observable seat states — is kept intact. Option 4 was rejected because `GET /shows/{id}` is contractually required to report `held`, and a status nothing can produce is a contract honoured on paper only; under option 3 all three states stay genuinely reachable and the contract is honoured by behaviour. Option 2 keeps the measurement problem. Option 1 keeps both problems and a worker.

Deleting the sweeper is safe precisely because it never carried correctness: LEARN-002's fourth probe showed a lapsed hold is claimable with no worker running. What the sweeper carried was *reporting*, and deriving status on read carries that better — it is exact at the instant of the read rather than exact to within one sweep interval.

**Consequences accepted**

- Without a sweeper, a lapsed hold's `reservation_seats` row stays active (`released_at IS NULL`) until that seat is next claimed, when the claim transaction closes it (ADR-019). A raw "count rows where `released_at IS NULL`" query therefore overcounts active claims between lapse and re-claim. That is a reporting wart, not a correctness defect: every reader that matters derives effective status instead, and the one reader that cannot — the backstop unique index — is handled by ADR-019. Any future reporting query must derive, not count.
- `reservations` rows for lapsed holds remain stored as `held`. Every reader derives `expired` from `(status='held' AND hold_expires_at <= now())`. The stored value is corrected only when the owner confirms or cancels, or never.
- Confirm and cancel must themselves carry the unexpired arm (`hold_expires_at > now()`), or a lapsed hold would still be promotable — the gap that a sweeper used to paper over. Specified in ADR-022.
- `ix_seats_expiring`, `ix_reservations_expiring` and `ix_idem_stale` lose their only reader and are dropped. Each was maintained on every write to serve a scan that no longer exists; `ix_seats_expiring` in particular was paid for on the hottest table in the system. A future cleanup job reintroduces what it needs, in its own migration.
- An optional cleanup job — closing lapsed holds' `reservation_seats` and `reservations` rows for reporting tidiness, and purging expired idempotency keys — is a later nicety with no correctness surface. Tracked as RISK-007 so growth is watched rather than forgotten.
- `reservations_expired_total` and the sweeper gauges leave the metric catalogue. `superseded_claims_closed_total` (ADR-019) replaces them as the observable signal that holds are lapsing.
- The hold path is now the *less* travelled path, so it needs deliberate test coverage rather than coverage by accident. `GET /shows/{id}` reporting `held`, confirm, cancel, and lapse-then-reclaim are each named tests.

## ADR-018 — No scope cuts

**Date** 2026-10-03

**Context** A set of cuts was offered to shorten the critical path: dropping refresh tokens (REQ-008), guest upgrade (REQ-004), sale windows, seat overrides and tiered pricing, the audit pipeline, and the rate limiter.

**Choice** Every one is kept. `GET /shows` remains deferred under the separately-approved ADR-015, which is unaffected.

**Reasoning** Each of these is a stated requirement with a traced acceptance criterion, and the design already carries the mechanism for it. Cutting them would reduce what the service does without making any remaining property hold more strongly — the correctness argument does not get shorter, only the feature list does.

**Consequences** The ticket board stays at its current breadth. `SEAT-051` (audit) and `SEAT-052` (rate limiting) remain off the critical path — nothing the burst measures depends on either — but they remain in scope and in their stage gates. This entry exists so the question is not reopened without a new ADR.

## ADR-019 — Superseded active-claim rows are closed inside the claim transaction

**Date** 2026-10-03

**Context** `uq_seat_active_claim ON reservation_seats (seat_id) WHERE released_at IS NULL` is the backstop that makes a second simultaneous active claim physically impossible. A lapsed hold is legitimately claimable, but its `reservation_seats` row is still active, so the legitimate new winner's `INSERT` violates the index and the *winner* receives a 409. Under ADR-017 there is no sweeper to close that row, so this is not an edge case — it is the expiry path.

**Options**

1. Tighten the index predicate to "active **and** unexpired". **Impossible**: a partial index predicate must be `IMMUTABLE`, and `now()` is `STABLE`. "Active and unexpired" is not expressible as an index condition (LEARN-008).
2. Drop the backstop index. Rejected: it is the only mechanism that is independent of the claim predicate, and ADR-001's consequences explicitly lean on that independence.
3. Keep a worker whose job is closing superseded rows. Rejected: it reintroduces the worker ADR-017 deleted, and no sweep interval is short enough — the race exists at any lag.
4. Close superseded rows inside the claim transaction, before inserting the new ones.

**Choice** Option 4. In T2, after the seat claim has succeeded and before the `reservation_seats` insert:

```sql
UPDATE reservation_seats SET released_at = now()
 WHERE seat_id = ANY($seat_ids) AND released_at IS NULL;
```

**Reasoning** By the time this statement runs, the transaction holds the row lock on every one of those `seats` rows and its predicate has proved each was available or lapsed-held. Any surviving active `reservation_seats` row for those seat ids therefore cannot belong to a live claim — if it did, the seat predicate would have failed and the transaction would already have declined. So the statement can only ever close a row that reality has already superseded, which is exactly what its name says.

The closure is a **separate statement**, not a data-modifying CTE combined with the insert. Sub-statements of one statement share a single snapshot and cannot see one another's effects, and their relative execution order is undefined (LEARN-009), so an insert folded into the same statement could be evaluated before the update and hit the index anyway. Two statements in order, in one transaction, is the only form that is correct.

Lock order is extended rather than altered: **no transaction locks a `reservation_seats` row for a seat whose `seats` row it does not already hold locked.** The `reservation_seats` locks are therefore totally covered by the `seats` locks taken in ascending label order, and no new cycle is constructible. The statement needs no `ORDER BY` for that reason, and a reviewer should not add one.

**Consequences** One additional statement in T2 on every reserve, raising the reserve path from roughly five round trips to six. It is served by `uq_seat_active_claim` itself — the backstop index is the index this statement needs — so it costs an indexed update of at most `per_user_limit` rows. Rows closed are counted as `superseded_claims_closed_total`, which is the observable measure of lapsed-hold churn that the deleted sweeper used to report. A backstop violation remains alert-worthy and remains logged at `error`: after this change there is no legitimate path that produces one.

## ADR-020 — Only successes are stored under an idempotency key; a decline releases it

**Date** 2026-10-03 · **Amends** ADR-010

**Context** Two statements six lines apart in `04-concurrency-and-atomicity.md` contradicted each other: "if T2 rolls back, the key remains `in_progress`" and "a domain decline is stored and replayed". A decline rolls T2 back, so there was no transaction in which the stored decline could be written.

**Options**

1. Store declines in a third transaction after T2's rollback, and replay them.
2. Store only successes; a domain decline releases the key so a retry genuinely re-attempts.

**Choice** Option 2.

**Reasoning** The requirement is that a key *reserves* exactly once, which option 2 satisfies: a released key can produce at most one reservation across any number of retries. Option 1 also satisfies it, but it is worse behaviour and worse engineering. Worse behaviour because a replayed stale decline is a lie — the seat may have freed in the interim, and the client is told it is still taken by a response recorded minutes ago. Worse engineering because it needs a *new* write-on-rollback path with its own failure story (if that write fails, the key is stuck), whereas releasing the key reuses the release mechanism ADR-010 already requires for the unexpected-5xx case, with the failure story already specified: if the release fails, the key stays `in_progress` and the staleness reclaim handles it.

**Consequences**

- `idempotency_keys` only ever holds `completed` rows whose outcome was a success, so `status_code` is always 2xx. The column is kept for forensics — it records what the original answer was — but the replay response status is fixed by ADR-029 regardless.
- The flow's "row disappeared" branch now has two causes: the owner faulted, or the owner declined. Both mean "genuinely re-attempt", and the retry remains bounded to one re-insert so two duplicates cannot ping-pong.
- A key provides no shielding against a client retrying into a sold-out seat: each retry is a real attempt. That is the correct semantic and the rate limiter, not the key, is what bounds the cost.
- REQ-024, REQ-026 and the reserve table in `06-apis.md` are updated to match.

## ADR-021 — The idempotency fingerprint carries the operation and the show id

**Date** 2026-10-03

**Context** The unique constraint is `(user_id, key)`; `scope` existed only as a column. The same key with the same seat labels against a *different show* therefore produced a matching fingerprint, and the service would replay the wrong show's reservation — the one failure mode the fingerprint exists to prevent.

**Options**

1. Add `scope` to the unique constraint, making the same key against a different show a distinct key that succeeds independently.
2. Fold the scope into the canonical fingerprint, making the same key against a different show a clean 409 `IDEMPOTENCY_KEY_REUSED`.

**Choice** Option 2. The fingerprint is SHA-256 over the canonical JSON of exactly:

```
{"op": "reserve", "show_id": "<path show id>", "seats": [sorted, de-duplicated],
 "hold_ttl_seconds": <integer, omitted when absent>}
```

Operation name and show id come from the route, not the body. The idempotency key itself, the request id, headers and any other field are excluded. Canonicalization rules are unchanged: keys sorted, whitespace normalized, labels sorted and de-duplicated, null-valued optionals omitted.

**Reasoning** A key identifies one attempt at one operation. A client that presents it against a second show has a bug, and the cheapest place to tell it so is the first request that does it. Option 1 silently accepts the bug and lets one key produce two reservations, which breaks the client's own model of what a key means even though the server's constraint is satisfied. Keeping the constraint at `(user_id, key)` also keeps the key space flat, so there is exactly one row to look up and one row to reclaim when stale.

**Consequences** `scope` remains a column, now purely diagnostic — it answers "what was this key used for" during an investigation — and is no longer load-bearing for correctness. `SEAT-031` and `SEAT-042`, which previously specified that the same key against a different show *succeeds*, are corrected. REQ-024 and REQ-025 state the fingerprint's contents, so a change to the request shape that forgets the fingerprint is a requirement failure rather than a silent behaviour change.

## ADR-022 — Cancel and confirm: reservation-row decision, ordered expiry-guarded seat update

**Date** 2026-10-03

**Context** The deadlock-freedom argument claimed cancel and confirm "acquire seat locks in ascending label order", but the statement specified for them was a plain `UPDATE seats ... WHERE reservation_id = $1`, which locks in scan order. A cancel of a multi-seat reservation racing a claim on the same seats can therefore produce a cycle, and the proof did not cover the SQL as written. Separately, neither statement carried the unexpired arm, so under ADR-017's lazy-only expiry a lapsed hold was still promotable.

**Choice** Both operations are two guarded statements in one transaction.

1. **The decision** is a guarded `UPDATE` on the `reservations` row, which both decides and serializes:

```sql
-- confirm
UPDATE reservations
   SET status='confirmed', confirmed_at=now(), hold_expires_at=NULL,
       updated_at=now(), request_id=$request_id
 WHERE id = $reservation_id AND user_id = $user_id
   AND status = 'held' AND hold_expires_at > now()
RETURNING id, show_id, seat_count, amount_paise, currency, confirmed_at;

-- cancel
UPDATE reservations
   SET status='cancelled', cancelled_at=now(), hold_expires_at=NULL,
       updated_at=now(), request_id=$request_id
 WHERE id = $reservation_id AND user_id = $user_id
   AND status = 'held' AND hold_expires_at > now()
RETURNING id, show_id, seat_count, cancelled_at;
```

2. **The effect** is the same ordered `FOR UPDATE` CTE shape as `claim_many`, with `ORDER BY label`, guarded on current ownership *and* on the unexpired arm, followed by closing the reservation's `reservation_seats` rows.

**Reasoning** Making the `reservations` row the decision point gives three things at once: the row lock serializes a principal's concurrent confirms and cancels, the `user_id` in the `WHERE` clause makes ownership a predicate rather than a fetch-then-compare, and the returned row count is the decision with no window between check and effect — the same shape as the claim. Carrying `hold_expires_at > now()` makes a lapsed hold unpromotable, which under lazy-only expiry is the whole of what expiry means for these two paths. `ORDER BY label` in the seat CTE puts cancel and confirm inside the deadlock argument instead of merely being asserted to be.

Lock order becomes three tiers, and the no-cycle argument covers every path: a claim takes quota → seats (ascending label) → `reservation_seats`; a cancel or confirm takes its own `reservations` row → seats (ascending label) → `reservation_seats`. No claim ever locks a `reservations` row (it only inserts one) and no cancel or confirm ever locks a quota row, so the two never contend at their first tier and always agree at the second.

**Consequences**

- Zero rows from statement 1 means the service must report *why*. It then re-reads the reservation owner-scoped, purely to choose the code: `cancelled` → 409 `RESERVATION_CANCELLED` (or 200 for a repeat cancel), `confirmed` → 200 for a repeat confirm, 409 `RESERVATION_CONFIRMED` for a cancel, effectively-expired → 409 `RESERVATION_EXPIRED`, absent or not owned → 404. That read is outside the lock and may observe a later state than the one that caused the decline; every state it can report is a state the reservation genuinely held, and the decision has already been made, so this is diagnosis rather than control.
- A shortfall in statement 2 (fewer seats promoted or released than `seat_count`) rolls the transaction back and returns 409 `SEAT_TAKEN`. The shortfall is believed unreachable — `now()` is transaction-stable (LEARN-007), so if the reservation is unexpired at that instant its seats are too — but it is asserted rather than assumed, because the argument depends on that Postgres semantic.
- Cancel of a lapsed hold is 409 `RESERVATION_EXPIRED`, not a successful no-op release. Its seats are already effectively available and its stale `reservation_seats` row is closed by the next claim under ADR-019, so nothing is leaked by refusing.
- REQ-031 and the cancel section of `06-apis.md` gain the expired case; `05-auth-and-rbac.md`'s hold-hijack row is corrected — it previously relied on `reservation_id` having moved on, which is true only for a seat that was *re-claimed*, not for one merely lapsed.

## ADR-023 — Framework-level failures answer inside the envelope

**Date** 2026-10-03

**Context** Stage 0 shipped with `GET /nope` returning Starlette's default `{"detail":"Not Found"}`, contradicting "one envelope for every failure". The registry had no code for an unmatched route or an unsupported method: 404 carried only `SHOW_NOT_FOUND`, `SEAT_NOT_FOUND` and `RESERVATION_NOT_FOUND`, and 405 carried nothing.

**Choice** Two codes are added to the registry — `ROUTE_NOT_FOUND` (404) and `METHOD_NOT_ALLOWED` (405) — and a `StarletteHTTPException` handler renders them in the standard envelope. The 405 response preserves the `Allow` header the router computed, because omitting it breaks the HTTP contract. Both are declines: `info` level, `fault=False`.

Any `StarletteHTTPException` whose status is neither 404 nor 405 is logged at `error` as `unhandled_http_exception` and answered 500 `INTERNAL_ERROR`. The only legitimate sources of that exception in this service are the router's own 404 and 405; anything else means application code raised `HTTPException`, which the conventions forbid, and surfacing it as a fault is how the violation gets noticed instead of quietly serving an unregistered error shape.

**Reasoning** "One envelope, always" is a client contract, and a client parsing `error.code` must not have to special-case two shapes depending on whether its URL typo reached a route. Naming the two outcomes also closes the error-code registry again, which is what makes the registry assertion in `SEAT-003` satisfiable (escalation 2).

**Consequences** `08-error-logging.md` gains an **operational codes** table listing the five codes that appear in no endpoint contract — `DATABASE_UNAVAILABLE`, `NOT_READY`, `INTERNAL_ERROR`, `ROUTE_NOT_FOUND`, `METHOD_NOT_ALLOWED` — so the registry assertion becomes an equality against *contract ∪ operational* rather than an unsatisfiable equality against contract alone. With trailing slashes deliberately not redirected, `/healthz/` now answers 404 `ROUTE_NOT_FOUND` in the envelope, which is the intended behaviour stated plainly.

## ADR-024 — The catch-all is a middleware boundary, not only an exception handler

**Date** 2026-10-03

**Context** An unhandled exception produced two log lines: ours, with the request id and a stack, and uvicorn's duplicate without one. Cause: registering a handler for `Exception` in Starlette installs it on `ServerErrorMiddleware`, which builds the response from the handler and then **re-raises unconditionally** so the server logs the failure (LEARN-010). `ServerErrorMiddleware` sits outside every user middleware, so the second line is emitted after the request-id ContextVar has been reset and cannot carry it.

**Options**

1. Accept the duplicate and record it as a risk.
2. Catch the exception in a middleware placed immediately inside `RequestContextMiddleware`, log it once with the request id and a stack, and return the envelope — so `ServerErrorMiddleware` sees an ordinary response and never re-raises.

**Choice** Option 2. `ExceptionBoundaryMiddleware` is registered so the effective inbound order becomes request context → **exception boundary** → access log → metrics → rate limit → audit → application. The `Exception` handler registration is retained as a second line of defence for anything raised in `RequestContextMiddleware` itself.

**Reasoning** Option 1 accepts a documented-invariant violation, not merely some noise: "every log line carries `request_id`" is a stated property of the log schema and the correlation story, and a line that cannot carry one makes the error stream a place where some faults are correlatable and some are not. Placing the boundary inside the request context instead gives every inner layer — routing, handlers, rate limiting, audit — exactly one error line, with the id, and leaves the genuinely uncoverable region (the context middleware itself) double-logged, which is appropriate for a fault in the thing that makes faults traceable.

**Consequences** One more middleware in the chain, on a path that only executes when something has already gone wrong. Tests asserting the 500 envelope must run the client with server exceptions not re-raised, since the exception no longer escapes the application. The access log sits *outside* the boundary, so a 500 produces an `unhandled_exception` line and an `http_request` line — two lines with different `event` values, both carrying the id, which is correlation rather than duplication.

## ADR-025 — `event_kind` is configuration, not an enum

**Date** 2026-10-03

**Context** `02-architecture.md` and `03-data-model.md` specify `event_kind` as free-form `TEXT` validated against configuration, chosen in ADR-013 so a new kind needs no migration. `SEAT-002` demanded an enum in `core/constants.py` and one was implemented, with members `CINEMA` and `CONCERT`.

**Choice** Configuration. `EventKind` is removed from `core/constants.py`; the column stays `TEXT`; the permitted set comes from `ALLOWED_EVENT_KINDS` and the default from `DEFAULT_EVENT_KIND`, both required settings with no code default, both validated at startup such that the default is a member of the permitted set.

**Reasoning** The generality discipline in `00-overview.md` is explicit: no vertical is named in a table, a column, or a module. `class EventKind(StrEnum): CINEMA, CONCERT` names two verticals in a module — it is precisely the thing the rule forbids, and it makes adding a third a code change and a deploy. ADR-013 already settled that the generic-across-verticals property is carried by a validated text column; the enum quietly reversed that without an ADR.

A code default is withheld deliberately: `{"cinema", "concert"}` as a fallback in `config.py` reintroduces the vertical names in code, with the extra defect that they would be invisible in the environment. `.env.example` carries them, which is where a vertical belongs.

**Consequences** `app/core/constants.py` loses `EventKind`. `app/core/config.py` gains `ALLOWED_EVENT_KINDS`, `DEFAULT_EVENT_KIND` and `DEFAULT_CURRENCY`, and `.env.example` gains all three. `shows.currency` loses its database default in the same change, so the value has exactly one source — the request, or `DEFAULT_CURRENCY` — rather than two that can disagree. Validation of `event_kind` lives in `domain/policies.py` against the configured set and returns 422 naming the field.

## ADR-026 — An idempotency waiter holds no connection between polls

**Date** 2026-10-03

**Context** A duplicate request that finds the key `in_progress` polls it for a bounded interval. As specified, nothing prevented that waiter from holding its pooled connection for the whole wait budget. At burst scale a few hundred concurrent duplicates would then occupy the pool for seconds, exhausting it and producing exactly the 503s ADR-016 forbids — a self-inflicted outage on the retry path.

**Choice** The waiter acquires a connection per poll, performs one point read, and **releases it before sleeping**. `idempotency_repo.get(user_id, key)` therefore takes no connection and acquires its own, the same signature trick that already enforces `try_claim`'s separate transaction.

**Reasoning** The wait is a client-facing latency budget, not a database-side one. Decoupling the two makes the waiter's cost one indexed point read per poll interval — round trips, which are cheap and bounded — instead of connection-seconds, which are the scarcest resource in the system. It also means the wait budget and the pool size can be tuned independently, where before raising one silently degraded the other.

**Consequences** `IDEMPOTENCY_WAIT_MS / IDEMPOTENCY_POLL_INTERVAL_MS` round trips per waiter in the worst case — at the current defaults, 40 point reads for a waiter that times out. Acceptable: each is an index lookup on `uq_idem_user_key`, and the alternative was pool exhaustion. `11-scalability.md` records that the pool-acquire timeout must still exceed the queue drain time, and that the waiter no longer contributes to that drain.

## ADR-027 — `lock_timeout` below `statement_timeout`, validated at startup

**Date** 2026-10-03

**Context** Two documents disagreed. `02-architecture.md` mapped both `lock_timeout` and `statement_timeout` exhaustion to 409; `08-error-logging.md` mapped `query_canceled` — which is what a statement timeout raises — to 503.

**Choice** They mean different things and map differently.

| Condition | SQLSTATE | Answer | Log level |
|---|---|---|---|
| `lock_timeout` exceeded | `55P03` `lock_not_available` | 409 `SEAT_TAKEN`, metric label `lock_timeout` | `warning` |
| `statement_timeout` exceeded | `57014` `query_canceled` | 503 `DATABASE_UNAVAILABLE` | `error` |

And the configuration is constrained so the distinction is real: `DB_STATEMENT_TIMEOUT_MS >= DB_LOCK_TIMEOUT_MS + DB_TIMEOUT_MARGIN_MS`, validated at startup, refusing the boot with both values named.

**Reasoning** A lock timeout on the claim path means another transaction holds the row — a contention outcome, which is a decline. A statement timeout means a statement could not finish in the time the database was given, which is a fault: the query plan is wrong, the database is overloaded, or something is pathological. Reporting a fault as a 409 tells the client a seat is taken when it may be free, and hides the fault.

The ordering invariant is what makes the mapping trustworthy. If `statement_timeout` were below `lock_timeout`, a contended claim would always be cancelled as a statement timeout before its lock timeout could fire, so every hot-seat decline would arrive as a 503 and REQ-048 would fail for a configuration reason with no code defect. The margin exists because a claim that waits out most of its lock budget still has work to do afterwards.

**Consequences** `app/core/config.py` gains `DB_TIMEOUT_MARGIN_MS` and the relationship check. `02-architecture.md`'s failure-posture line is corrected. A 503 on the claim path is now unambiguous evidence of a genuine fault, which is what makes it alertable.

## ADR-028 — Admin endpoints reject unknown fields; every other endpoint ignores them

**Date** 2026-10-03

**Context** `06-apis.md` required unknown body fields to be rejected with 422 everywhere, citing `user_id` as the reason. REQ-005 and the burst's phase-7 spoof probe require a reserve carrying another principal's id to *succeed*, owned by the token's subject — "can only ever act as the token's user" implies it acts. The two cannot both hold.

**Choice** An asymmetry, stated as a rule: **admin endpoints use `extra="forbid"`; every other endpoint uses `extra="ignore"`.**

**Reasoning** The two audiences fail differently. An admin's `POST /shows` turns a request body into durable configuration — prices, per-user limit, hold TTL, per-seat overrides — and a mistyped field name there silently produces a show that sells the wrong seats at the wrong price, discovered by customers. Failing loudly is much cheaper than that. A buyer's request has its effect determined entirely by the path, the token subject and the named fields; a stray field cannot change the outcome, and rejecting it converts a harmless client quirk into a failed booking during exactly the on-sale the service exists for.

Identity safety does not rest on this policy at all, which is the point worth keeping separate: **no request model anywhere declares an identity field**, so there is nothing in any schema for a body value to bind to. Spoofing is impossible by construction rather than by validation, and would remain impossible if the extras policy changed tomorrow.

**Consequences** `06-apis.md`'s conventions row is rewritten, `12-testing-and-burst.md`'s "unknown body fields are rejected 422" integration check is replaced by two checks (ignored on reserve, rejected on `POST /shows`), and `SEAT-034` no longer expects 422 for a body carrying `user_id` — it expects 201 owned by the token subject. The schema-introspection test from `SEAT-014` is widened from authenticated models to **every** request model, and gains a second assertion: each model's `extra` setting matches its route's audience.

## ADR-029 — Every idempotent replay answers 200

**Date** 2026-10-03

**Context** REQ-024 and `04-concurrency-and-atomicity.md` said a replay returns the original status code; `06-apis.md` listed 200 for a replay. With 201 as the original status, the two readings differ in a way that matters: a retry replayed as 201 is indistinguishable from a second successful claim.

**Choice** Every replay answers **200** with the stored body and `Idempotent-Replay: true`, never 201.

**Reasoning** Exactly-one-201-per-hot-seat is a property the burst measures by counting status codes. If a replay can answer 201, that count is no longer a count of claims and the headline property becomes unmeasurable — the same failure of measurability that motivated ADR-017. 200 is also the honest status: nothing was created by this request. Under ADR-020 only successes are stored, so there is no replayed-decline case to reconcile with this rule.

**Consequences** REQ-024 and the replay line in `04-concurrency-and-atomicity.md` are corrected to match `06-apis.md`. `idempotency_keys.status_code` records the original outcome for forensics and no longer determines the replay's status. A client distinguishing "created" from "already created" reads the header, which is what it is for.

## ADR-030 — The burst harness is a track that opens with Stage 0

**Date** 2026-10-03

**Context** The burst script sat entirely in Stage 7, after every other stage. It is the artifact that produces the evidence every correctness claim rests on, and it was scheduled last — so a defect in the *measurement* would be discovered at the point where there is no time left to fix it.

**Choice** The burst becomes **Track B**, running in parallel from Stage 0. `SEAT-055` — configuration and flags, the bounded HTTP client, the report renderer, and the **exit contract** with its violation-injection self-tests — opens alongside Stage 0 and depends only on `SEAT-001`. It needs no application server: the exit-contract tests feed synthetic phase results, which is the right way to test a gate anyway. `SEAT-056` (phases 1–7) opens as its endpoints land, phases 1–2 green at Stage 3 and 3–7 at Stage 4. `SEAT-057`–`SEAT-060` stay in Stage 7, where phase 9's metric agreement first becomes checkable.

**Reasoning** The script is buildable against the documented HTTP contract before the service exists, so nothing about it needed to wait. Building the exit contract first inverts the risk that matters: a script that prints a distribution but exits zero on a violation is worse than no script, and that defect is only findable by deliberately injecting violations — which is exactly what `SEAT-055` now does, months before the real violations could occur.

**Consequences** The critical path gains a parallel lane and `SEAT-055` leaves its tail. Every Track B dependency still points at a lower-numbered ticket, so the board's no-forward-dependency rule holds without renumbering. Stage 7 keeps the full-scale live run (`SEAT-059`) as the stage that produces the evidence; what moved earlier is the machinery, not the verdict.

## ADR-031 — Canonical forms for the two startup relationship checks

**Date** 2026-10-03

**Context** `13-deployment.md` required startup to validate that "guest token lifetime exceeds hold TTL plus a margin" and that "the acquire timeout exceeds the expected queue drain". Neither quantity is derivable from the declared settings: there is no single hold TTL (a hold may request any value up to the show maximum), and queue drain depends on burst size and round trips per request, neither of which is configuration. Stage 0 implemented two concrete forms instead.

**Choice** Both implemented forms are blessed as canonical, with their reasoning written into `13-deployment.md`.

| Check | Form | Status |
|---|---|---|
| Guest token outlives a hold | `GUEST_TOKEN_TTL_SECONDS > MAX_HOLD_TTL_SECONDS` | Necessary bound |
| Acquire timeout versus statement timeout | `DB_ACQUIRE_TIMEOUT_SECONDS * 1000 > DB_STATEMENT_TIMEOUT_MS` | Necessary condition |
| Timeout ordering (new, ADR-027) | `DB_STATEMENT_TIMEOUT_MS >= DB_LOCK_TIMEOUT_MS + DB_TIMEOUT_MARGIN_MS` | Sufficient |

**Reasoning** `MAX_HOLD_TTL_SECONDS` is the right quantity because it is the longest hold the service will ever issue, so a configuration failing this check is one in which *no* hold could reliably be confirmed by a guest. It is necessary, not sufficient: a token minted shortly before a maximum-length hold can still lapse first, which is RISK-006 rather than something a startup check can see.

The acquire-versus-statement check is likewise the necessary half of what the prose asked for. An acquire timeout below the statement timeout guarantees refusals under any contention at all — a request would be turned away while the single statement ahead of it was still legitimately running — so a configuration violating it is certainly wrong. The sufficient condition cannot be a startup check because its inputs are not settings; it is established by measurement in `SEAT-058` and the chosen numbers recorded in the ledger.

**Consequences** A startup check is now explicitly labelled necessary or sufficient, so nobody mistakes the first kind for a guarantee. `13-deployment.md` carries all three forms verbatim, and `12-testing-and-burst.md`'s config unit test asserts each one rejects a violating environment.

---

## RISK-006 — A guest token can expire before an opt-in hold it created

**Trigger** A guest requests a hold whose TTL exceeds the remaining lifetime of its access token. ADR-031's startup check bounds the configuration, not the individual request.

**Impact** The guest cannot confirm. The hold then lapses and the seats return to the pool.

**Mitigation** Accepted. Under ADR-017 the default reserve confirms immediately, so a guest buying a ticket never meets this; only a guest that deliberately opts into a hold can. No seat is lost — the hold lapses and the seat is re-sellable — and the guest may take a fresh guest identity and reserve again. The available fix, clamping the hold TTL to the token's remaining lifetime, is deferred because it couples the claim path to token internals for a case that costs one retry.

## RISK-007 — Lapsed-hold rows and expired idempotency keys accumulate

**Trigger** ADR-017 removes the sweeper, so nothing closes a lapsed hold's `reservation_seats` row until that seat is next claimed, nothing moves a lapsed `reservations` row to `expired`, and nothing purges `idempotency_keys` past `IDEMPOTENCY_RETENTION_HOURS`.

**Impact** Growth, and a raw "count active claims" query overcounts. No correctness property is affected: every reader that matters derives effective status, and the backstop index is kept truthful by ADR-019.

**Mitigation** Watch `idempotency_keys` row count and table size. Retention is a documented maintenance query in the `13-deployment.md` runbook, backed by `idempotency_repo.purge_expired`, run on a schedule rather than by an in-process loop. A single cleanup job covering all three concerns is the fix when growth justifies it — one job with no correctness surface, which is a much easier thing to add later than a sweeper would have been to remove.

---

## LEARN-007 — `now()` is transaction-stable, and the expiry argument depends on it

**Date** 2026-10-03

`now()` is an alias for `transaction_timestamp()`: it returns the start time of the current transaction and is therefore **constant for the whole transaction**. `clock_timestamp()` is the moving one.

**Why it is load-bearing** Three separate arguments in this design rest on it:

- Every expiry test inside one transaction agrees. ADR-022's confirm reads `reservations.hold_expires_at > now()` and then promotes seats guarded on `seats.hold_expires_at > now()`; because both evaluate the same instant, a reservation that is unexpired cannot own a seat that is expired, which is what makes the shortfall case unreachable.
- ADR-019's superseded-row closure uses the same instant as the claim predicate that preceded it, so a row the predicate judged lapsed is still judged lapsed when it is closed.
- All expiry arithmetic uses the database clock, never the application's, so clock skew between app and database cannot affect any boundary (the first row of the threats table in `04-concurrency-and-atomicity.md`).

**Implication** Any statement in an expiry-sensitive path uses `now()`. Substituting `clock_timestamp()` to be "more precise" would break the agreement between a transaction's own statements, which is the opposite of precision.

## LEARN-008 — A partial index predicate must be `IMMUTABLE`, so `now()` cannot appear in one

**Date** 2026-10-03

PostgreSQL requires expressions in an index definition, including a partial index's `WHERE` predicate, to be `IMMUTABLE`. `now()` is `STABLE`, so `... WHERE released_at IS NULL AND hold_expires_at > now()` is rejected outright.

**Implication** "Active **and** unexpired" is not expressible as an index condition. The backstop index can only express "active", which is why a lapsed-but-open claim row collides with a legitimate new one and why ADR-019 closes superseded rows in the claim transaction instead of tightening the index. Anyone revisiting the backstop should read this before proposing the index fix — it is the first idea everyone has, and the database refuses it.

## LEARN-009 — Data-modifying CTEs share one snapshot and have no defined order

**Date** 2026-10-03

Sub-statements inside one `WITH` clause execute with the **same snapshot** and cannot see one another's effects on the target tables; their relative execution order is not defined. Two data-modifying sub-statements touching the same rows produce unpredictable results.

**Implication** ADR-019's closure of superseded `reservation_seats` rows cannot be folded into the same statement as the insert of the new ones — the insert could be evaluated first and violate `uq_seat_active_claim` regardless. The two must be separate statements, in order, in one transaction. This generalizes: a data-modifying CTE is correct for "update these rows and return what changed", and wrong for "do A, then do B because A happened".

## LEARN-010 — `ServerErrorMiddleware` re-raises unconditionally

**Date** 2026-10-03 · **Found by** backend-dev during Stage 0

Registering an exception handler for `Exception` in Starlette does not install a normal handler. The handler is passed to `ServerErrorMiddleware`, which uses it to build the response and then re-raises the exception so the ASGI server logs it. `ServerErrorMiddleware` is installed outside every user middleware, so the re-raise is logged after `RequestContextMiddleware` has reset the request-id ContextVar — producing a second JSON line for the same fault with no correlation id.

**Implication** A catch-all registered as an exception handler cannot be the only catch-all if "every log line carries `request_id`" is to hold. ADR-024 moves the boundary into a middleware placed immediately inside the request context, which catches, logs once with the id and a stack, and returns the envelope so nothing escapes to be re-raised. Any framework upgrade should re-check this behaviour, and any test asserting the 500 envelope must disable the test client's own exception re-raising.

## LEARN-011 — `COPY --chmod` needs BuildKit, which the local daemon does not use

**Date** 2026-10-04 · **Found by** backend-dev during SEAT-005

Colima's Docker daemon uses the legacy builder, which rejects `COPY --chmod`:

```
the --chmod option requires BuildKit
```

**Implication** The Dockerfile uses `COPY` followed by `RUN chmod 0755`, which builds on both the legacy builder and BuildKit. Any Dockerfile change in this repository must build on the legacy builder, because that is what the development machine has — a `--chmod`, `--link` or heredoc that only works under BuildKit will pass in CI and fail locally, which is the worse direction for that failure to point.

## RISK-008 — Transitive dependencies are not hash-pinned

**Trigger** A transitive dependency publishes a release incompatible with the pinned direct set.

**Impact** `mds/13-deployment.md` specifies "Lockfile, hash-pinned". All 12 direct dependencies in `pyproject.toml` are `==`-pinned, but the 19 transitive ones (`anyio`, `starlette`, `cffi`, `watchfiles`, …) resolve fresh at build time. Two consecutive builds were verified to resolve identically, which demonstrates determinism **today**, not reproducibility next month. A clean checkout months from now could fail to build or build differently — and REQ-050 is exactly the requirement that a clean checkout builds.

**Mitigation** Accepted for now rather than fixed: the direct pins bound the blast radius, and a hash-pinned lockfile duplicates the dependency list into a second file that must be kept coherent. Recorded rather than silently tolerated. If a build ever resolves differently, generate a hash-pinned lock and make the install `--require-hashes`; that is the fix and it is well understood. Revisit during Stage 7 hardening, where build reproducibility is already in scope.

**Note** This is the one SEAT-005 acceptance check that is not honestly satisfiable as written. The ticket's `Done when` should read "direct dependencies are exactly pinned" with this risk referenced, rather than claiming a lockfile that does not exist.

---

## Stage 0 review outcome (SEAT-007)

**Date** 2026-10-04

Three blockers, nine high, twelve medium. Fixed in this stage: the build-system defect (B1), the DSN crash-loop (B2), empty-secret boot (B3), redaction bypassed by key shape and by free text (H5, H6), two missing relationship checks (H8), the `.env.example` parity test (H9), and three tests that passed against broken implementations (M1, M2, M9).

Eleven categories were attacked and found clean, including secrets in the image (verified on the built artifact with `docker save | strings`), cold-cache build, hardcoded tunables, duplicated error codes, ContextVar leakage, handler leakage and layering.

## RISK-009 — Redaction does not reach arbitrary object types

**Trigger** A log call passes a dataclass, a Pydantic model, a `set`, or any non-`dict`/`list` object in `extra=`.

**Impact** `_redact` recurses only into dicts and lists. `orjson` serialises dataclasses natively and `default=str` stringifies everything else, so a field named `password` inside a dataclass reaches the log line without passing the key filter. Stage 2's principal and credential DTOs are exactly this shape.

**Mitigation** The key filter now matches atoms as substrings, which covers dict-shaped payloads. Normalising dataclasses and models (`dataclasses.asdict`, `model_dump`) before filtering is the fix and is owed before Stage 2 introduces those DTOs. Free-text credentials in `message` and `stack` are scrubbed by pattern; an arbitrary secret pasted into an exception message is not pattern-matchable and is covered by the convention not to put one there.

## RISK-010 — Log writes are synchronous on the event loop

**Trigger** The container's stdout consumer stops draining — log-shipper backpressure, disk pressure on the node.

**Impact** `StreamHandler(sys.stdout)` with `PYTHONUNBUFFERED=1` means one unbuffered `write(2)` per record on the event-loop thread. Demonstrated: once a 64 KB pipe buffer fills, the process produces no output and an `asyncio.wait_for` timer never fires, because the blocking write prevents the loop from reaching the timer callback. Every in-flight claim stalls, the healthcheck times out, the platform restarts mid-burst, and `unhandled_exceptions_total` stays at zero through the outage.

**Mitigation** Owed before Stage 7's burst work: route records through `QueueHandler` + `QueueListener` on a worker thread with a bounded queue and a drop policy — the same shape as the audit write path, and for the same reason.

## RISK-011 — `alembic.ini` can never exist in the image

**Trigger** SEAT-008 adds `alembic.ini` at the repository root.

**Impact** The runtime stage copies `app/` only, so the entrypoint's `[ -f alembic.ini ]` guard stays false in the image forever while being true on every developer machine. `alembic upgrade head` would silently never run in the container, with a `migrations_skipped` warning as the only signal — reopening RISK-005 by accident.

**Mitigation** SEAT-008 must either `COPY alembic.ini ./` or invert the guard to an explicit `SKIP_MIGRATIONS` flag so the default is to fail loudly. Recorded now because the failure is invisible at the moment it is introduced.

## RISK-012 — The log schema has a second implementation in bash

**Trigger** `entrypoint.sh` emits a hand-rolled JSON line before Python starts.

**Impact** It carries four of the six mandated fields and second-rather-than-microsecond precision, so it already violates the schema every Python line is tested against. A contract defined in two places that have diverged.

**Mitigation** Accepted while the only such line is `migrations_skipped`. If the entrypoint ever needs a second line, it emits through Python instead.

## LEARN-012 — A mutation-covered suite can still contain vacuous assertions

**Date** 2026-10-04

The Stage 0 suite was validated against 14 deliberate mutations, every one of which killed tests — and it still contained three assertions that proved nothing: a loop that iterated zero times because the formatter had already neutralised the values it scanned for, a test whose name promised stack redaction while its body raised an exception containing no secret, and no test at all coupling `.env.example` to `Settings` (two fields were deleted and the suite stayed green).

**Implication** Mutation coverage proves the mutations were caught, not that every assertion has teeth. The gap is assertions whose *premise* is false — they never execute, or they assert something adjacent to the property named. Two checks worth running on any suite that matters: confirm each assertion actually executes (a loop over a filtered collection can be empty), and confirm the test's name matches what its body asserts. Both found defects here that mutation testing structurally could not.

## ADR-032 — Deadline scope: build the claim path, cut what does not touch it

**Date** 2026-10-04

**Context** The design was complete and nothing behind `POST /shows/{id}/reserve` existed, with the deadline the same day.

**Choice** Build register/login/guest, show create and read, reserve, confirm, cancel, a reservation read, `/readyz` and `/metrics`. **Not built:** the audit table and writer, rate limiting, `/auth/upgrade`, `/auth/refresh`, `GET /shows`, `GET /reservations`, sale windows, `seat_overrides`, and the HTTP/pool/lock-wait histograms. `ACCESS_TOKEN_TTL_SECONDS` defaults to a day because there is no refresh flow. The schema is one Alembic revision without `audit_log`.

**Consequences** REQ-046 (audit) and the rate-limit requirements are unmet and say so in the write-up. `RATE_LIMIT_*` and `AUDIT_*` settings remain declared and have no reader. Three deliberate departures from the documents, each smaller than what they replace: `idempotency_repo` functions take a connection (the service passes one that is not in a transaction, which is the property the no-connection signature existed to enforce); `GET /shows/{id}` derives its counts from the same statement that returns the seat rows rather than from a second counts query, which is strictly stronger; and `reservations_confirmed_total` counts every reservation that reaches `confirmed`, with `reservations_held_total` beside it, in place of `reservations_created_total{show_id,kind}` — REQ-042 names "reservations confirmed", and `show_id` is not a bounded label.

## LEARN-013 — The `alembic` console script does not put the working directory on `sys.path`

**Date** 2026-10-04

`python -m alembic` does, so `env.py` importing `app.core.config` worked on every local run and failed with `ModuleNotFoundError: No module named 'app'` the first time the real image booted. Fixed with `prepend_sys_path` in `app/alembic.ini`.

**Implication** Found only by running `docker compose up`, not by any test. A migration path is verified when the entrypoint has run it in the image, and not before.

## LEARN-014 — asyncpg's pool issues `RESET ALL` on release

**Date** 2026-10-04

A `SET statement_timeout` in a pool `init` hook would be reverted the first time the connection was returned. The guards are passed as `server_settings` — startup parameters — which are what `RESET ALL` resets *to*. `test_session_guards_are_applied_and_survive_release` covers it.

## LEARN-015 — `jsonb` does not preserve key order

**Date** 2026-10-04

A replayed response is the stored `jsonb` body, so it is equal to the original as JSON and not byte-for-byte. A test comparing response text failed on a correct replay. Clients comparing replays must compare parsed bodies.

## RISK-009, RISK-010, RISK-011 — closed

**Date** 2026-10-04

- **RISK-009** `_redact` now normalises pydantic models and dataclasses before filtering keys.
- **RISK-010** Log records are rendered on the calling thread and written by a `QueueListener` thread through a bounded queue that drops when full (`LOG_QUEUE_MAX`).
- **RISK-011** `alembic.ini` lives in `app/`, the entrypoint runs the migration unconditionally and fails the boot if it fails, and the compose stack and the CI container job both exercise it against a real database.

## RISK-013 — CI and compose secrets were below the validated minimum

**Date** 2026-10-04

`JWT_SECRET` in `docker-compose.yml` and `.github/workflows/ci.yml` was shorter than the 32 characters `Settings` requires, so neither would have booted. Fixed. CI has still never run on a real runner; the first push is the test.

## ADR-033 — Restore four cut features; make key ownership survive a wrong "presumed dead"

**Date** 2026-10-04

**Context** ADR-032 cut scope to reach a working claim path. With that deployed and passing a live burst, the four cuts that cannot affect booking were restored. Writing the test for stale-key recovery then exposed a defect in the recovery itself.

**Choice** Built: `/auth/upgrade`, `/auth/refresh` (access TTL back to 15 minutes), `seat_overrides` on show creation, `GET /shows` and `GET /reservations` (keyset on `(created_at, id)`), and the router's 404/405 inside the envelope (SEAT-066, less its single-log-line clause). Still not built: audit, rate limiting, sale windows, the HTTP/pool/lock-wait histograms. Supersedes the corresponding lines of ADR-032.

Idempotency: a reclaim now **rotates the key's id**, and T2 begins by locking the key row by id. `complete` and `release` address the id, so a reclaimed owner can neither commit nor delete the new owner's key.

**Consequences** One extra row lock per reserve, on a row only that principal's duplicates touch. `mds/04` carries the argument. `mds/15-tickets.md` remains stale against ADR-017/020/021/028 in several "Done when" lines and is not the authority where they disagree.

## LEARN-016 — A test at the degenerate setting found what the realistic one would not

**Date** 2026-10-04

Stale-key recovery was tested first with `IDEMPOTENCY_STALE_SECONDS=0` as a shortcut. Every in-progress key is then stale the instant after it is taken, so eight concurrent duplicates reclaimed from one another, and the result was two owners for one key: one reservation, several 409s, and a `release` by a loser deleting the row the winner held. At the real setting (30s against a T2 bounded to a few seconds) this needs a stalled process to reproduce, which is exactly the situation the recovery exists for.

**Implication** The guard on a recovery path has to hold when the premise "the owner is dead" is false. Test recovery paths with the owner alive.

## LEARN-017 — Adversarial review of the claim path: what broke and what did not

**Date** 2026-10-04

One `grill` round over the reserve, confirm, cancel and idempotency code, executing probes against PostgreSQL rather than arguing.

**Could not break** No double-sell across ~6,400 randomized reserve attempts with one-second holds, confirms and cancels timed at the expiry boundary; hot seat with 2,500 principals gave exactly one 201; 400 parallel requests from one principal against a limit of 4 gave exactly 4; 600 concurrent requests on one key gave one reservation; ~5,700 mid-churn samples of the reconciliation invariant all held; no deadlock surfaced.

**Broke, all fixed with a test each**
- A lock timeout on confirm or cancel returned 500: only the claim statements were wrapped by the contention translation. It now wraps every statement on those paths that can wait on a row lock.
- A NUL character in a seat label or idempotency key returned 500. Text fields now reject control characters, and `asyncpg.DataError` is a 422 at the session boundary as a backstop.
- A validly signed token whose subject has no user row returned 500, because the foreign key fires at the key insert, before the place that translated it.
- A test in this suite ran `UPDATE reservation_seats SET released_at = now()` with no `WHERE`, releasing every claim row in the test database and disarming the backstop for anything running beside it. Scoped to its own seat.

**Implication** "Nothing on this path may return 5xx" was true of the statements that were examined and false of their neighbours. The rule is per statement that can wait or reject, not per path.

## RISK-014 — The per-user limit is per principal, and principals are free

**Trigger** A client mints guests in a loop: `POST /auth/guest` is unauthenticated and unthrottled (rate limiting is not built, ADR-032), and a reserve confirms with no payment step.

**Impact** One client can take N × `per_user_limit` seats. The lock-and-count mechanism is sound; what it counts is not a person. A client re-holding with a one-second TTL can likewise squat inventory.

**Mitigation** None in the service. REQ-047's per-IP ceiling on guest issuance is the designed control and is the first thing to build next.

## RISK-015 — A failed key release blocks that key for the staleness window

**Trigger** T2 fails on a saturated pool, and the release that follows cannot get a connection either.

**Impact** Retries with that key answer 409 `IDEMPOTENCY_IN_PROGRESS` for up to `IDEMPOTENCY_STALE_SECONDS` (30s). Waiters poll every 50ms, adding pool round trips exactly when the pool is short. No incorrect reservation results.

**Mitigation** Accepted. A waiter backoff is the improvement.

## RISK-016 — A hold's expiry is measured from transaction start

**Trigger** A claim asking for a very short hold waits on a lock for longer than that hold.

**Impact** `now()` is the transaction's start (LEARN-007, which the expiry argument depends on), so the hold can be committed already lapsed: the client receives 201 `held` for a seat others can claim at once. No double-sell. Separately, a confirm that loses its seat to a superseding claim at the boundary answers 409 `SEAT_TAKEN` where `RESERVATION_EXPIRED` would be more accurate.

**Mitigation** Accepted. A minimum hold TTL above `DB_LOCK_TIMEOUT_MS` would close the first.

## ADR-034 — Build the rate limiter; RISK-014 is bounded, not closed

**Date** 2026-10-04

**Context** The review made RISK-014 concrete: 2,500 guests minted from one client in seconds, each entitled to `per_user_limit` seats, with no payment step.

**Choice** `RateLimitMiddleware` as specified in `07-middleware.md`: a monotonic token bucket per `(identity, route class)` in a bounded LRU, keyed by verified principal wherever a token is present and by client address for `auth` and `guest`. 429 carries `Retry-After` and the `X-RateLimit-*` headers in the standard envelope; `rate_limited_total{route_class}` counts it; `/readyz` reports whether limiting is on. Supersedes the rate-limiting line of ADR-032.

Three departures from the document, each stated:
- The `auth` class is keyed by address alone, not address and email: the email is in the body, and the limiter does not read bodies.
- The client address is the entry `RATE_LIMIT_TRUSTED_PROXY_HOPS` from the **right** of `X-Forwarded-For`. The leftmost entry is whatever the client chose to send. A 429's details name the address the limit was applied to, so a wrong hop count is visible from outside as an internal address.
- The deployment and the compose stack set `RATE_LIMIT_GUEST=600/600s` rather than the default `60/60s`. The sustained rate is the same, one a second; the bucket is deeper so the burst can mint its principals from one address.

**Consequences** RISK-014 is bounded at one new guest per second per address, so `per_user_limit` seats per second per address — not closed. A payment step or a verified identity is what closes it. RISK-003 (per-instance buckets) now applies for real.

## LEARN-018 — The right-most `X-Forwarded-For` entry on Render is the platform's, not the client's

**Date** 2026-10-04

With `RATE_LIMIT_TRUSTED_PROXY_HOPS=1`, thirty concurrent bad logins against the live service produced 429s whose `details.limited_by` read `address 10.25.16.5` and `address 10.26.34.133` — internal addresses of the platform's own proxies. Every client was being resolved to one of a handful of shared buckets: the `auth` ceiling of ten a minute was, in effect, global.

Locally there is no proxy and the socket peer is the client, so no test could have shown this.

**Implication** The hop count is a property of the deployment and can only be verified against it. That the limiter names the address it applied a limit to is what made a wrong value visible from outside in one request; without it the symptom would have been "users report being throttled". The correct value is found by raising the setting until `limited_by` shows the caller's own public address, and it must be re-checked if the platform's edge changes.

## ADR-035 — The documents describe the code; one document describes what is not built

**Date** 2026-10-04

**Context** The design set was written before the build, then the build cut scope (ADR-032), restored some (ADR-033, ADR-034) and departed from the documents in small ways. The documents described a service with an audit trail, a metrics middleware, a gauge refresher and background workers, none of which exist.

**Choice** Documents 02–13 are rewritten or edited to describe the service as built. Everything designed and unbuilt, and everything found to be needed, is in `17-future-scope.md` and nowhere else. `14-stage-plan.md` states per stage what is built, tested and reviewed. `15-tickets.md` keeps the original board with a status table and a note that its `Done when` lines predate several ADRs. Settings with no reader (`AUDIT_*`, `GAUGE_REFRESH_SECONDS`, `LOG_SAMPLE_DEBUG`) are removed from the code and `.env.example`.

**Consequences** A statement in 02–13 is a claim about the code and can be checked against it. This ledger is not rewritten: earlier entries describe what was decided at the time, and are superseded by later ones rather than edited.

## LEARN-019 — On Render the client is three entries from the right of `X-Forwarded-For`

**Date** 2026-10-05

Completes LEARN-018, by test against the live service from one machine:

| `RATE_LIMIT_TRUSTED_PROXY_HOPS` | Observed |
|---|---|
| 1 | 429s named `10.25.16.5` and `10.26.34.133` — the platform's internal proxies. Every client shared a bucket |
| 2 | 700 requests in a few seconds against a ceiling of 300 per 10s: **none** limited. The resolved address differed per request, so every request had its own bucket and the limiter was in effect off |
| 3 | 500 requests: 89 limited, each naming the machine's own public address. Repeated with a different forged `X-Forwarded-For` on every request: still limited, still the real address |

**Implication** Both wrong values failed silently in opposite directions — one throttled everyone together, the other throttled no one — and neither is visible from a test suite, a health check or a passing burst. The value is recorded in `render.yaml` with how it was established. Between the deploy of the limiter and this fix, the live service's rate limiting was first shared across all clients and then ineffective.

## ADR-036 — The web page is three static files served by the service itself

**Date** 2026-10-05

**Context** The service had no page, and `02-architecture.md` listed a UI as out of scope. A page was asked for. The options were a separate static site with its own deployment and CORS on the API, or files served by the existing service.

**Choice** `app/static/` holds `index.html`, `app.css` and `app.js`; `GET /` returns the page and `/static/*` its assets. No framework, no build step. Both paths are exempt from rate limiting, so loading the page spends none of the visitor's `read` allowance. A missing asset answers `ROUTE_NOT_FOUND` in the envelope (ADR-023 still holds). The plan and what was built are in `18-frontend.md`.

**Consequences** One URL and one deployment; the image changes by three files. The page adds nothing to the booking logic and uses only existing endpoints. It polls `GET /shows/{id}` every four seconds per open tab, which is `read` traffic keyed by principal. Guest issuance and sign-in are limited per client address, so the page depends on `RATE_LIMIT_TRUSTED_PROXY_HOPS` being right on Render; it is, as of LEARN-019.

## ADR-040 — Cancel releases a confirmed reservation

**Date** 2026-10-05

**Context** ADR-022 and REQ-031 made cancel a hold-only operation: a confirmed reservation answered 409 `RESERVATION_CONFIRMED`. ADR-017 then made confirm the default outcome of a reserve. Together they meant the ordinary sequence — book a seat, cancel it — was refused. The original brief asks for a cancel after which the seat is cleanly re-bookable, and its own example reservation is `confirmed`. The handover had said to release `held` or `confirmed`; the documents were followed instead, which was the wrong authority.

**Choice** `cancel_owned` matches a confirmed reservation or a live hold. `release_for_reservation` releases seats that carry this reservation's id and are active. Nothing else changes: ownership is still a `WHERE` clause, a non-owner still gets 404, a lapsed hold still answers `RESERVATION_EXPIRED`, a repeat cancel is still 200.

**Why it is safe** The release is guarded on `reservation_id`. A confirmed seat cannot lapse, so it cannot have been claimed by anyone else while still naming this reservation; and after the release it names nobody, so a repeat cancel matches zero rows and cannot touch a seat's next owner. Lock order is unchanged: the reservation row, then seats ascending by label.

**Consequences** Supersedes the cancel half of ADR-022 and rewrites REQ-031. There is no refund or cancellation window, because there is no payment. `RESERVATION_CONFIRMED` is removed from the registry: nothing can raise it any more.

## ADR-041 — The admin account follows configuration, and the demo's is published

**Date** 2026-10-05

**Context** Reviewers must create a show to test anything, and the admin sign-in existed only in a private dashboard. The earlier bootstrap created an admin once and never touched it again, so the live account could not be changed by configuration either.

**Choice** At every start the service upserts the admin: the account named by `ADMIN_EMAIL` exists, is an admin, and has `ADMIN_PASSWORD`. For the demo those are `admin@example.com` / `seat-admin-2026`, published in the README and the write-up.

**Consequences** Anyone can act as admin on the demo: create shows, read the audit trail and logs. Bounded by what an admin can do — nothing deletes or edits, and no secret reaches a log line. An earlier admin under a different email is not demoted. This is a demo decision and is the opposite of what a real deployment needs; the write-up says so.

## ADR-042 — Build the audit trail and an admin console; logs readable from it

**Date** 2026-10-05

**Context** The brief asks for log access, or a recording, and weights observability equally with correctness. Render's log stream is private. REQ-046 (audit) had been cut (ADR-032).

**Choice** The audit trail as designed — bounded buffer, drop and count when full, batched writer on a dedicated connection, drained on shutdown — with one simplification: a `deque` checked against a maximum rather than an `asyncio.Queue`, since nothing ever awaits it. Per-request facts (who, show, seats) are noted by the code that knows them into a per-request dict, so the middleware reads no body. An admin console at `/admin` reads aggregates over the trail, the trail itself, and an in-memory tail of the process's own redacted log lines. Supersedes the audit line of ADR-032.

**Consequences** Two connections are now kept back from the pool. The console's own requests are not audited. The log tail is per process and empties on restart. Latency per route is available in the console from the trail; as a Prometheus histogram it is still future scope.

## LEARN-020 — The time is not in the handler

**Date** 2026-10-05

The audit trail measures duration at the innermost layer. Over a local burst of 580 reserves it recorded p50 5 ms and p95 6 ms for the reserve route, while the burst's clients measured p50 125 ms and p95 400 ms for the same requests; at 5,000 in flight clients saw p50 11 s.

**Implication** Under load the wait is in front of the handler — connections queued on one Python process — not in the database and not on row locks. The lever is CPU and process count, not SQL. It is also why the free instance, with a fraction of a CPU, serves about 19 bookings a second however the queries are written.

## ADR-037 — The web page requires an account to book; the API still accepts guests

**Date** 2026-10-05

**Context** The page gave every visitor a guest session and let it book. A guest is identified only by its token: one hour, no refresh token (`05-auth-and-rbac.md`), kept in the tab's session storage. So a guest's confirmed ticket became unreachable when the tab closed or the hour passed. The seat stayed sold; no one could see, show or cancel the ticket. Found by the user, using the live page.

**Options** (a) Ask for a mobile number at booking. Without an OTP it is a string anyone can type, so knowing a number would be enough to read or cancel its tickets, which breaks owner-only access; with one it needs an SMS provider. (b) Give guests a refresh token. Keeps tickets on one browser only, and reverses the decision that a guest session is bounded. (c) Require an account at the moment of booking, in the page.

**Choice** (c). Browsing and seat selection need no session. Book and Hold open the account dialog when signed out, keep the selection, and carry out the booking once the person has registered or signed in. The page no longer calls `POST /auth/guest` or `/auth/upgrade`.

**Consequences** A ticket bought through the page is tied to an account and reachable from any device by signing in. No visitor creates a `users` row by loading the page. The API is unchanged: REQ-003 and REQ-004 still hold, and the burst and the seed script still book as guests by default. `burst.sh --accounts` books as registered accounts instead, the way a page visitor does; registration shares the `auth` ceiling with login (ten a minute per address), so at the default setting that mode suits a few dozen buyers and a full-size run needs a deeper `RATE_LIMIT_AUTH` bucket on the target. This does **not** close RISK-014 — registration costs no more than a guest did, so the per-user limit is still per free principal. An anonymous visitor's reads are now rate limited by address rather than by principal.
