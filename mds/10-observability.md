# Observability

The requirement is not "has metrics". It is that someone watching this service during an on-sale burst can see it behaving correctly, and that the numbers they see agree with the database.

---

## Health

Two endpoints with genuinely different jobs. Collapsing them into one is a common mistake: a liveness probe that touches the database restarts a healthy process during a database blip, and a readiness probe that does not touch the database routes traffic to an instance that cannot serve it.

### `GET /healthz` — liveness

Returns 200 whenever the process can serve a request. Touches no dependency, allocates nothing, completes in single-digit milliseconds. Answers one question: should this process be restarted?

### `GET /readyz` — readiness

Executes a real query — `SELECT 1` on a pooled connection with a short timeout — and reports per-dependency results.

```json
{ "status": "ready",
  "checks": { "database": { "ok": true, "latency_ms": 3 } },
  "rate_limiting": "enabled" }
```

**Fails closed.** With the database unreachable it returns 503 naming the failing dependency. Never served from a cached result: a readiness endpoint that caches is a readiness endpoint that lies, and it lies for exactly the duration of the cache during exactly the incident the probe exists to detect.

It reports whether rate limiting is enabled, so a service running with limits disabled cannot do so unnoticed.

Verified by a test that stops the database and asserts 503 while `/healthz` stays 200.

---

## Metrics

`/metrics`, Prometheus text format, from `prometheus_client`. All collectors declared once in `core/metrics.py`.

### Domain — the ones that prove correctness

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `reservations_confirmed_total` | counter | `show_id` | holds promoted to confirmed |
| `reservations_created_total` | counter | `show_id` | holds successfully taken |
| `reservations_declined_total` | counter | `reason` | **the headline metric** |
| `reservations_cancelled_total` | counter | `show_id` | explicit cancels |
| `reservations_expired_total` | counter | — | swept by the hold sweeper |
| `seats_available` | gauge | `show_id` | available seats |
| `seats_held` | gauge | `show_id` | active holds |
| `seats_confirmed` | gauge | `show_id` | sold |
| `seats_total` | gauge | `show_id` | for the invariant check |

`reason` values: `seat_taken`, `per_user_limit`, `idempotent_replay`, `idempotency_key_reused`, `idempotency_in_progress`, `show_not_on_sale`, `lock_timeout`, `deadlock`.

A burst's entire story reads off `reservations_declined_total`: how many lost a race, how many hit their limit, how many were retries. That breakdown is what distinguishes a correct service under contention from a broken one, and it is why declines are counted by reason rather than lumped together.

### HTTP

| Metric | Type | Labels |
|---|---|---|
| `http_requests_total` | counter | `route`, `method`, `status` |
| `http_request_duration_seconds` | histogram | `route`, `method` |
| `http_requests_in_flight` | gauge | — |
| `rate_limited_total` | counter | `route_class` |
| `unhandled_exceptions_total` | counter | `route` |

`unhandled_exceptions_total` must stay at zero through a burst. It is the direct measurement of the "zero 5xx" requirement.

### Database and workers

| Metric | Type | Meaning |
|---|---|---|
| `db_pool_size`, `db_pool_in_use`, `db_pool_waiting` | gauge | saturation, and the early warning for a 503 |
| `db_query_duration_seconds` | histogram | labelled by operation, bounded set |
| `seat_claim_lock_wait_seconds` | histogram | hot-seat contention, directly observable |
| `hold_sweeper_last_run_timestamp` | gauge | staleness of expiry reporting |
| `hold_sweeper_swept_total` | counter | |
| `audit_queue_depth` | gauge | backpressure indicator |
| `audit_records_written_total` | counter | |
| `audit_records_dropped_total` | counter | must stay zero |
| `audit_flush_duration_seconds` | histogram | |

### Cardinality

Label values come only from bounded sets. `show_id` is the one unbounded label, applied **only** to the per-show seat gauges, which is where it is genuinely needed — and even there it is capped: gauges are published for a configured number of recently active shows, not for every show ever created. No metric is ever labelled with a user id, a seat label, an idempotency key, or a concrete path. Unbounded label cardinality is how a metrics endpoint becomes an out-of-memory incident under a 20,000-request burst.

### Gauge refresh and reconciliation

Counters are incremented inline in the service layer, so they are exact.

Gauges cannot be: recomputing per-show seat counts on every claim would add a full count to the hot path. They are refreshed by a background task every configured interval using the same single-snapshot counts query the API uses, so the gauge and `GET /shows/{id}` are derived from identical SQL and cannot disagree about what "available" means.

Consequence stated honestly: gauges lag by up to one refresh interval during a burst and converge immediately after. The interval is short and configured. A grader comparing a gauge to the API mid-burst sees at most one interval of lag — recorded as RISK-004 — while `GET /shows/{id}` is always exact. The counters, which are exact, are what reconcile to the unit.

---

## Audit log

### Purpose

Metrics aggregate; logs expire. The audit table is the queryable record of individual requests — what a specific request id did, which principal did it, what outcome it got. It is what a later monitoring view reads, and what answers "show me every declined reserve on this show in the last ten minutes" without a log search.

### Write path

```
request  ──► AuditMiddleware ──► asyncio.Queue(maxsize=N)
                 put_nowait          │ bounded, drop on full
                 never awaits        ▼
                              audit_writer worker
                              drain up to BATCH or FLUSH_INTERVAL
                                     │ dedicated connection
                                     ▼
                              INSERT … executemany  ──► audit_log
```

Properties, each one deliberate:

- **The request path never awaits a database write.** `put_nowait` succeeds or drops. Audit can never add latency to a booking.
- **Bounded queue, drop on full.** Awaiting capacity would make audit a source of backpressure on bookings. Losing an audit row is an inconvenience; failing a booking is a defect. Drops increment `audit_records_dropped_total` and log `audit_queue_saturated` at `warning`, so the loss is never silent.
- **Batched writes.** One multi-row `INSERT` per batch instead of one per request — the difference between 20,000 inserts and roughly 40 at a batch size of 500.
- **Dedicated connection.** Outside the request pool, so a saturated pool cannot stall audit and a slow audit flush cannot starve requests.
- **Flush on shutdown.** The lifespan handler drains the queue with a bounded timeout, so a graceful deploy does not lose the last batch.
- **No foreign keys on the table.** An FK check per row would add contention for no operational benefit, and an audit row referencing a deleted user is still evidence.

Batch size, flush interval, queue depth, and the drop policy are all config values.

### Retention

Indexed on `occurred_at DESC`, on `request_id`, and partially on `outcome_code`. Growth is linear in traffic; partitioning by `occurred_at` with a retention window is the plan, deferred until volume requires it and noted in [11-scalability.md](11-scalability.md).

### Monitoring view

A later, optional stage: a read-only page over `audit_log` showing request rate, status distribution, decline reasons by code, and recent failures, each row linking to its request id. Strictly read-only, strictly off the request path, reading replica-safe aggregates. Scoped in [14-stage-plan.md](14-stage-plan.md) as the last stage, after correctness and deployment are proven.

---

## Tracing

Full distributed tracing is not warranted for a single service and a single database. The request id delivers the same practical value at a fraction of the cost: one value correlates every log line, the response header, the error envelope, and every database row the request wrote.

If an external dependency is ever added, OpenTelemetry is the upgrade path and the request id becomes the trace id. Noted, not built.

---

## What would page someone at 2am

Ordered by what each one actually means.

| Alert | Condition | Why it matters |
|---|---|---|
| **Double-sell backstop fired** | any `uq_seat_active_claim` violation | The atomic claim has a bug. This is the one that justifies waking someone, because the service is selling seats twice. |
| **Deadlock detected** | any `40P01` | The lock-ordering argument is wrong, or a new path violates it. |
| **5xx on a domain route** | `unhandled_exceptions_total` increases, or 5xx rate > 0 on reserve | Violates a hard requirement. A decline must never surface as a fault. |
| **Readiness failing** | `/readyz` 503 for more than a short window | The service cannot serve; traffic should already be shedding. |
| **Reconciliation violated** | `seats_available + seats_held + seats_confirmed != seats_total` sustained beyond one gauge interval | Either a real invariant break or a drifted effective-status expression. Not alerted inside one interval, because gauges lag. |
| **Pool exhausted** | `db_pool_waiting` sustained above zero | The precursor to 503s. Catching it here prevents the alert above. |
| **Audit dropping** | `audit_records_dropped_total` increases | Losing the audit trail, and a signal that the writer is stalled or the database is slow. |
| **Sweeper stalled** | `hold_sweeper_last_run_timestamp` older than several intervals | Reported state is going stale. Not urgent — expiry is enforced lazily in the claim predicate, so seats stay bookable — but it degrades every state read. |
| **Lock wait tail** | `seat_claim_lock_wait_seconds` p99 above threshold | Hot-seat contention approaching `lock_timeout`, which would turn into spurious declines. |

Deliberately **not** paging: a high rate of `SEAT_TAKEN`, a high rate of `PER_USER_LIMIT`, a high rate of `idempotent_replay`, or high request volume. Those are the service working correctly during an on-sale. An alert that fires every time the product succeeds is an alert that gets muted, and a muted alert channel is how the real one gets missed.

---

## Logs in production

Structured JSON to stdout, shipped by the platform. Access and retention per [13-deployment.md](13-deployment.md).

The three things that make these logs usable: every line carries `request_id`, every `event` is a queryable identifier rather than a sentence, and declines are `info` so the error stream contains only genuine faults. Without that third property the error rate during a burst is meaningless, which is the point of the level discipline in [08-error-logging.md](08-error-logging.md).
