# Observability

The requirement is not "has metrics". It is that someone watching this service during an on-sale burst can see it behaving correctly, and that the numbers they see agree with the database.

---

## Health

Two endpoints with different jobs. Collapsing them is a common mistake: a liveness probe that touches the database restarts a healthy process during a database blip, and a readiness probe that does not touch it routes traffic to an instance that cannot serve.

### `GET /healthz` — liveness

200 whenever the process can serve a request. Touches no dependency. Answers one question: should this process be restarted? The image's `HEALTHCHECK` uses it.

### `GET /readyz` — readiness

A real `SELECT 1` on a pooled connection, bounded by `READYZ_TIMEOUT_SECONDS`.

```json
{ "status": "ready", "rate_limit_enabled": true,
  "checks": { "database": { "ok": true, "latency_ms": 3.1 } } }
```

**Fails closed.** With the database unreachable it returns 503 with `status: "not_ready"` and `checks.database.error`, and logs `readiness_check_failed`. `error` is the *class name* of the root cause, never its message: a driver error routinely carries the connection string it failed with. Never served from a cached result — a readiness endpoint that caches lies for exactly the duration of the cache, during exactly the incident it exists to detect.

`rate_limit_enabled` is reported so a service running with limiting switched off cannot do so unnoticed. Render's health check path is `/readyz`.

---

## Metrics

`GET /metrics`, Prometheus text format. Every collector is declared once in `core/metrics.py`.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `reservations_confirmed_total` | counter | — | reservations that reached `confirmed`, by a direct reserve or by confirming a hold |
| `reservations_held_total` | counter | — | reserves that opted into a hold |
| `reservations_declined_total` | counter | `reason` | **the headline metric**: reserve requests that created nothing |
| `reservations_cancelled_total` | counter | — | reservations cancelled by their owner |
| `superseded_claims_closed_total` | counter | — | claim rows closed because a lapsed hold's seat was re-claimed (ADR-019) |
| `rate_limited_total` | counter | `route_class` | requests refused with 429 |
| `unhandled_exceptions_total` | counter | `route` | exceptions that reached the catch-all handler |
| `audit_records_written_total`, `audit_records_dropped_total` | counter | — | audit rows written; rows lost to a full buffer or a failed flush |
| `audit_queue_depth` | gauge | — | audit records buffered, at scrape time |
| `seats_available` | gauge | `show_id` | seats a claim would succeed on right now |

`reason` is the lower-cased error code — `seat_taken`, `per_user_limit`, `show_not_on_sale`, `idempotency_key_reused`, `idempotency_in_progress`, `seat_not_found`, `show_not_found`, `validation_error` — or one of four that are not codes: `lock_timeout`, `deadlock`, `active_claim_backstop`, and `idempotent_replay`. A replay is a success for the client; it is counted here because it is a reserve request that created nothing, which is what the metric measures.

A burst's whole story reads off `reservations_declined_total`: how many lost a race, how many hit their limit, how many were retries. That breakdown is what distinguishes a correct service under contention from a broken one.

`unhandled_exceptions_total` must stay at zero through a burst. It is the direct measurement of the "zero 5xx" requirement (ADR-016).

Nothing counts a hold *expiring*: with no sweeper there is no moment at which the service notices a lapse, only moments at which readers derive it. `superseded_claims_closed_total` is the honest signal — lapsed holds whose seats were actually re-sold, measured where the work happens.

### Cardinality

Label values come only from bounded sets. `show_id` is the one unbounded label, applied only to `seats_available`, and capped: the gauge is published for the `GAUGE_MAX_SHOWS` most recently created shows. No metric is ever labelled with a user id, a seat label, an idempotency key or a concrete path.

### The gauge is computed at scrape time

Counters are incremented inline in the service layer, so they are exact. The gauge is read from the database **when `/metrics` is scraped**, by the same predicate the claim uses, so the gauge and `GET /shows/{id}` cannot disagree about what "available" means and there is no refresh interval for them to disagree across. If the database does not answer, the counters are still served and `metrics_gauge_unavailable` is logged.

The cost is one grouped count per scrape. A scrape every few seconds over fifty shows is negligible beside a burst.

Specified but not exposed — the HTTP latency histogram, pool gauges, the lock-wait histogram and the other three per-show seat gauges — are item 3 of [17-future-scope.md](17-future-scope.md).

---

## Tracing

Distributed tracing is not warranted for one service and one database. The request id delivers the same practical value: one value correlates every log line, the response header, the error envelope, and every row the request wrote.

---

## What would page someone at 2am

Every row starts from a signal that exists today.

| Alert | Signal | Why it matters |
|---|---|---|
| **Double-sell backstop fired** | `claim_backstop_violated` at `error`; `reservations_declined_total{reason="active_claim_backstop"}` | The atomic claim has a bug. This is the one that justifies waking someone |
| **Deadlock** | `claim_deadlock` at `error`; `reason="deadlock"` | The lock-ordering argument is wrong, or a new path violates it |
| **5xx on a domain route** | `unhandled_exceptions_total` increases | A decline surfaced as a fault. Violates a hard requirement |
| **Readiness failing** | `/readyz` 503; `readiness_check_failed` | The service cannot serve |
| **Lock-timeout storm** | `reason="lock_timeout"` climbing | Hot-seat queues are exceeding `lock_timeout` |
| **Audit dropping** | `audit_records_dropped_total` increases | The writer is stalled or the database is slow. Bookings are unaffected by design, so nothing else will say so |
| **Throttling real users** | `rate_limited_total` climbing on `reserve` or `read` | A ceiling is too low for the traffic, or the proxy-hop count is wrong and clients are sharing a bucket |

Deliberately **not** paging: a high rate of `seat_taken`, `per_user_limit` or `idempotent_replay`, a rising `superseded_claims_closed_total`, or high volume. Those are the service working correctly during an on-sale. An alert that fires every time the product succeeds gets muted, and a muted channel is how the real one gets missed.

Two alerts the design wants and cannot yet raise before the fact, because their signals are not exposed: pool exhaustion (`db_pool_waiting`) and the lock-wait tail.

---

## Logs

Structured single-line JSON to stdout, shipped by the platform. Records are rendered on the calling thread — where the request context is — and written by a listener thread through a bounded queue that drops when full, so a stalled log consumer cannot block the event loop (RISK-010, closed).

Three things make these logs usable: every line carries `request_id`, every `event` is a queryable identifier rather than a sentence, and declines are `info` so the error stream contains only genuine faults ([08-error-logging.md](08-error-logging.md)).

## Audit trail

One row per request in `audit_log` ([03-data-model.md](03-data-model.md)): who, which show, which seats, status, duration, outcome code, request id.

```
request ──► AuditMiddleware ──► bounded buffer ──► writer task ──► audit_log
             append or drop                         batches, own connection
```

Each property is deliberate:

- **The request path never waits.** `enqueue` appends or drops. Audit cannot add latency to a booking.
- **A full buffer drops and counts.** Awaiting space would make audit a source of backpressure on bookings. Losing an audit row is an inconvenience; failing a booking is a defect. `audit_records_dropped_total` makes the loss visible.
- **Batched writes on a dedicated connection**, outside the request pool.
- **Drained on shutdown**, within a bound.
- **No foreign keys.**

Proven by a test that sizes the buffer to one, fires thirty bookings at once, and asserts all thirty succeed while twenty-nine records are dropped.

Metrics: `audit_records_written_total`, `audit_records_dropped_total`, `audit_queue_depth`.

## Admin console

`GET /admin` serves a page; everything it shows comes from four admin-only endpoints ([06-apis.md](06-apis.md)).

| Tab | Source |
|---|---|
| Overview | `audit_log` aggregated over a window — requests, status classes, declines by code, latency p50/p95 per route, requests per minute — plus this process's counters, pool usage and audit buffer |
| Shows | create a show; recent shows with availability from the claim's own predicate |
| Audit trail | `audit_log`, filtered, newest first; a request id links to its log lines |
| Logs | the in-memory tail of this process's log lines |

**The log view** is the answer to "logs access" on a platform whose own log stream is private. A handler on the log writer thread keeps the newest `LOG_BUFFER_MAX` rendered lines — the same lines that reached stdout, so already redacted. It is per process and empties on restart; the audit trail is the durable record.

Latency per route lives here, from the audit trail, rather than as a Prometheus histogram on `/metrics`; the histogram is still future scope.
