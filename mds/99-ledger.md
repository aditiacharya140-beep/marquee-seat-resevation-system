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

**Context** The brief permits either an explicit cancel or a time-boxed auto-expiring hold, and requires `GET /shows/{id}` to report available, held, and confirmed.

**Choice** Reserve returns `held` with `expires_at`. `POST /reservations/{id}/confirm` promotes to `confirmed`. `cancel` releases early. Expiry is enforced lazily in the claim predicate and eventually by a sweeper.

**Reasoning** All three seat states genuinely exist and are observable, which the state contract demands. A hold is also where a payment step would attach without reworking the claim. Both release models are implemented, which is strictly more than either alone.

**Consequences** Reserve returns `status: "held"`, not `"confirmed"` as in the brief's literal example response. Deliberate and documented in the README and `WRITEUP.md`, since the brief explicitly invites choosing a hold model. Confirm must be reachable before a token expires, hence the guest-token-lifetime validation.

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

**Context** Rate limiting is required, and the graded burst sends 20,000 requests in roughly one second.

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

**Reasoning** SQLite cannot express `SELECT … FOR UPDATE`, has different predicate re-evaluation semantics, and does not support partial unique indexes the same way. The single thing being graded is precisely the part that would differ between engines, so it would be untested until it reached production.

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

**Impact** A reviewer's first request takes seconds. An expired database makes the live URL return 503 — a dead deploy, which the brief names as the most common way strong submissions fail.

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
