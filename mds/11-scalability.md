# Scalability

Stated plainly: the design trades peak throughput for correctness, because correctness is the requirement and throughput is a constraint to survive rather than a number to maximize. This document says where the ceilings are, which one is hit first, and what is deliberately not solved.

---

## The contention model

Three regimes, with very different costs:

| Regime | Shape | Cost | Scales with |
|---|---|---|---|
| **Cold** | 20,000 buyers, distinct seats | One row lock each, no contention | Pool size and CPU |
| **Warm** | Thousands of buyers, a few hundred popular seats | Short lock queues per row | Database write throughput |
| **Hot** | 500 buyers, one seat | One 500-deep lock queue | Serialized — the critical section's duration |

Only the hot regime is fundamentally serial, and it is serial **by definition**: one seat has one winner, so determining that winner cannot be parallelized. The engineering goal is therefore not to parallelize it but to make each critical section as short as possible and to bound the queue.

The critical section for a single-seat claim is one indexed update on one row. The queue drains at roughly the rate the database can commit that update. `lock_timeout` bounds the tail so a request at the back of a long queue declines cleanly rather than hanging, and `seat_claim_lock_wait_seconds` makes the depth observable.

A seat that is contested is also, necessarily, a seat that will be gone in milliseconds. 499 of those 500 requests are going to be declined no matter how fast the system is. Spending complexity to decline them faster is spending it in the wrong place.

---

## Per-request cost

The reserve path, counted honestly:

| Step | Round trips | Notes |
|---|---|---|
| Idempotency key claim (T1) | 1 | own transaction; required for correctness |
| Quota row upsert + lock | 1 | combined statement |
| Active-seat count | 1 | index-only scan, bounded rows |
| Seat claim | 1 | the atomic decision |
| Close superseded claim rows | 1 | ADR-019; served by `uq_seat_active_claim`, ≤ `per_user_limit` rows |
| Reservation + seat links + key completion | 1 | multi-statement, one round trip |
| **Total** | **~6** | two transactions |

Roughly six round trips, two transactions, one contended row lock held for one of them. The quota lock is held across the count and the claim, which is what makes the limit exact — a cost paid knowingly.

The sixth step was added by ADR-019 and is unconditional. Making it conditional would require reading first to see whether there is anything to close, which is a read-then-write on exactly the state the claim just decided — the forbidden shape. An unconditional indexed update on at most `per_user_limit` rows is cheaper than the read that would avoid it.

A **declined** reserve costs less: the decline short-circuits before the claim or at it, so a loser of a hot-seat race pays the key claim, the quota lock, the count and the claim, then the key release — and 499 of 500 requests for a contested seat are losers. The expensive path is the one that succeeds, which is the right way round.

The clear optimization available is collapsing the limit check and the claim into one statement with a `NOT EXISTS` guard, removing a round trip and the quota lock entirely. It is not done, because the quota lock is also what serializes a single principal's concurrent requests, and without it the limit becomes checkable-but-not-enforceable. Recorded as ADR-006: correctness over a round trip.

---

## Connection pool arithmetic

This is where the "zero 5xx" requirement is won or lost, and it is arithmetic rather than tuning.

```
usable_connections   = db_max_connections − superuser_reserved − migration/admin headroom
pool_per_instance    = usable_connections / instance_count − audit_writer_connection
```

The pool is sized by what the **database** can serve, never by expected request concurrency. Postgres backends are processes; a pool larger than the database's ceiling converts a queue the application controls into refusals the application cannot control.

Excess concurrency therefore queues **at the pool**, which is the right place for it: the wait is bounded by an acquire timeout, it is visible as `db_pool_waiting`, and a request that exceeds it returns 503 `DATABASE_UNAVAILABLE` — the single legitimate 5xx in the service. Keeping it at zero under a full-scale burst is a sizing exercise, and `db_pool_waiting` sustained above zero is the alert that precedes it.

A 20,000-request burst against a pool of, say, 20 means a queue roughly 1,000 deep. At ~6 round trips of a few milliseconds each, that drains in single-digit seconds. **The acquire timeout must exceed that drain time**, or correct requests are refused for a queue that was about to serve them. This is the one number most likely to produce a spurious 5xx under load, so it is configured generously and measured by the burst script.

That requirement cannot be a startup check, because drain time depends on burst size and round trips per request and neither is configuration. What *is* checked at startup is the necessary half — `DB_ACQUIRE_TIMEOUT_SECONDS * 1000 > DB_STATEMENT_TIMEOUT_MS`, since an acquire timeout below the statement timeout guarantees refusals under any contention at all. The sufficient condition is established by measurement in `SEAT-058` and the chosen numbers recorded in the ledger. ADR-031 labels each check necessary or sufficient so the first kind is not mistaken for a guarantee.

**Idempotency waiters do not contribute to the drain.** A duplicate request waiting on an `in_progress` key releases its connection between polls (ADR-026), so a wait budget costs round trips rather than connection-seconds. Before that fix, a few hundred concurrent duplicates would have held connections for the whole budget and produced the 503s ADR-016 forbids — the retry path taking the service down.

---

## Horizontal scaling

The service is stateless. Every instance behaves identically because the atomic decision is entirely in the database and nothing in the claim path holds in-process state.

What changes with N instances:

| Component | Effect |
|---|---|
| Claim correctness | Unaffected. The decision is a database row lock. |
| Connection pool | Divided by N. This is what bounds N, not CPU. |
| Rate limiting | Per-instance, so the effective ceiling becomes N × configured. Accepted; see below. |
| Expiry | Nothing to scale. It is a predicate arm inside the claim, so it runs exactly as often as a claim does and has no worker, no interval, and no leader election (ADR-017). |
| Audit writer | One per instance with its own connection and its own queue. Independent and correct. |
| Gauge refresher | Runs on every instance; each publishes its own view. Scraped per instance, aggregated by the collector. |

**What breaks first as N grows is database connections, not application CPU.** The fix at that point is a connection pooler in transaction mode, not more instances. Beyond that, the next ceiling is write throughput on `seats`, and beyond that the architecture must change — sharding by show, since shows are perfectly independent. Noted as the real scaling axis, not built.

### Rate limiting is per-instance, deliberately

With N instances the effective ceiling is N × the configured value. Accepted: the limiter is abuse protection, not a quota system, and no correctness property depends on it. An exact global limiter requires Redis — a new dependency, a new failure mode, and a network round trip on the hot path — in exchange for precision nothing needs. ADR-007, RISK-003. The token-bucket interface is the seam if an exact global quota is ever required.

---

## Consistency and availability under partition

The service is deliberately **CP with respect to seat allocation**. A seat is a unique physical thing; selling it twice is not a degraded experience, it is a wrong answer that someone must be refunded and apologized to for.

| Partition | Behaviour |
|---|---|
| App cannot reach the database | `/readyz` 503, requests return 503 `DATABASE_UNAVAILABLE`. **Unavailable, never inconsistent.** |
| Database primary fails over | Writes fail during the window and return 503; the pool reconnects; no claim is lost or duplicated because uncommitted transactions roll back. |
| One of N instances is partitioned | Removed by the readiness probe; the rest continue. No shared in-process state to reconcile. |
| Client cannot read a response | The idempotency key makes the retry safe. This is the common real partition and it is handled by design rather than by availability. |

There is no mode in which this service accepts a reservation it cannot durably record. Queueing claims to accept writes during a database outage would mean accepting bookings without knowing whether the seat is free, which is the double-sell this entire design exists to prevent.

The honest trade: a database outage is a full booking outage. For assigned seating, that is the correct choice. Reads could be served from a replica during such an outage, showing stale availability — deliberately not done, because stale availability on a booking page produces failed bookings and user-visible inconsistency for very little gain.

---

## Known ceilings

| Ceiling | Current limit | First symptom | Next step if hit |
|---|---|---|---|
| Hot-seat serialization | One winner per seat per commit | `seat_claim_lock_wait_seconds` p99 climbing toward `lock_timeout` | Inherent. Shorten the critical section; nothing else. |
| Database connections | Postgres `max_connections` | `db_pool_waiting` above zero, then 503s | Connection pooler in transaction mode |
| Seat write throughput | Primary's commit rate | Latency rise across all claims | Shard by show |
| Audit insert rate | Batched writer throughput | `audit_queue_depth` climbing, then drops | Larger batches, then table partitioning |
| `audit_log` growth | Linear in traffic | Index bloat, slow queries | Partition by `occurred_at` + retention |
| `idempotency_keys` growth | One row per reserve that won its key | Row count and table size climbing with no purge running | Scheduled `purge_expired` maintenance; RISK-007 |
| Lapsed-hold rows left open | One per hold that lapsed and whose seat was never re-claimed | A raw count of `released_at IS NULL` exceeding real active claims | The same deferred cleanup job; readers derive effective status and are unaffected |
| Show creation | One multi-row insert for N seats | Slow `POST /shows` for very large halls | `COPY` for very large seat sets |
| Metric cardinality | Per-show gauges | `/metrics` response size and memory | Already capped to recently active shows |
| Free-tier instance | One small instance | Cold starts, CPU saturation | Scale up, which the design already supports |

---

## Deliberately not solved

| Not solved | Reason |
|---|---|
| Exact global rate limiting | Needs Redis; no correctness depends on it |
| Read replicas | Would show stale availability and risk apparent invariant violations |
| Queue-based admission control | A virtual waiting room is a product decision, not a correctness one, and would add a component that can itself fail |
| General admission (unassigned) capacity | A guarded counter decrement is a different mechanism needing its own correctness argument; out of scope |
| Multi-region | A single primary is the premise of the atomic decision; multi-region writes need a different design entirely |
| Caching show state | Availability is the most volatile data in the system; a cache would serve wrong answers during exactly the burst it was added for |

Each of these is a reasonable next step for a larger system. None of them makes the correctness properties hold more strongly, and several would weaken them.
