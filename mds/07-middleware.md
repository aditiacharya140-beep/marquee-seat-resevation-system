# Middleware

## Chain order

ASGI middleware wraps outward, so registration order in `main.py` is the reverse of execution order. The effective inbound order is:

```
1. RequestContextMiddleware      mint/accept request id, bind context
2. AccessLogMiddleware           time the request, log the outcome
3. MetricsMiddleware             latency histogram, in-flight gauge
4. RateLimitMiddleware           per-principal ceilings, 429 + Retry-After
5. AuditMiddleware               non-blocking enqueue of an audit record
6. application                   routes, Depends, services
```

The rationale for each position:

- **Request context is first** so every later layer — including a rate-limit rejection and an unhandled exception — has a request id to log and return. A 429 with no correlation id is an untraceable event, which is the opposite of the point.
- **Access log and metrics wrap the rate limiter**, so throttled requests appear in latency and status metrics. A limiter that hides its own rejections from the metrics makes a throttling incident invisible.
- **Rate limiting precedes audit and the application** so a rejected request costs nothing beyond the bucket check — no audit row, no database connection, no route resolution.
- **Audit is innermost** so it records the final status code, which it can only know on the way out.

Error handling is **not** middleware. FastAPI exception handlers render the envelope, because they have access to the resolved route and the raised `AppError`. A thin outermost catch-all exists only for exceptions escaping before the handler stack is reachable.

---

## RequestContextMiddleware

Establishes correlation for everything downstream.

```
inbound X-Request-ID present and parses as UUID  →  adopt it
otherwise                                        →  mint uuid4
set ContextVar request_id
→ call downstream
set response header X-Request-ID
reset ContextVar
```

- An inbound id is validated as a UUID before adoption. An arbitrary client string would end up in a `UUID` database column and in log fields, so a malformed value is replaced with a minted one rather than rejected — the client's malformed header is not worth failing a booking over.
- Adopting a client-supplied id lets a caller correlate across its own retries, which is exactly what a burst script needs.
- The `ContextVar` is read by the logger, the repository layer (to stamp `request_id` on every row), and the audit middleware. Nothing passes the id through a function signature.
- The token is reset in a `finally`, so a leaked `ContextVar` cannot bleed into the next request on the same task.
- Background workers set their own id per batch, so sweeper and audit-writer activity is traceable too.

---

## AccessLogMiddleware

One structured line per request, emitted on the way out:

```json
{"ts":"…","level":"info","event":"http_request","request_id":"…",
 "method":"POST","path":"/shows/…/reserve","route":"/shows/{show_id}/reserve",
 "status":409,"duration_ms":7,"user_id":"…","outcome_code":"SEAT_TAKEN"}
```

`route` is the path template, not the concrete path, so a burst against 20,000 distinct show ids groups into one series instead of exploding cardinality. The concrete `path` is kept for forensic lookup.

Health and metrics endpoints are excluded by default — a platform health check every few seconds otherwise dominates the log volume and buries the signal.

---

## MetricsMiddleware

Records, labelled by `route`, `method`, and `status`:

- `http_request_duration_seconds` — histogram, buckets configured for a sub-50ms service
- `http_requests_total` — counter
- `http_requests_in_flight` — gauge, incremented on entry and decremented in a `finally`

Label values come only from the route template and a bounded status set. No label ever carries a user id, a seat label, a show id, or an idempotency key — unbounded label cardinality is how a metrics endpoint becomes a memory leak under a 20k burst.

Domain outcome counters are incremented in the **service** layer, not here, because middleware cannot distinguish `SEAT_TAKEN` from `PER_USER_LIMIT` without parsing a body.

---

## RateLimitMiddleware

### Design

In-process token bucket, one bucket per `(principal_or_ip, route_class)`, monotonic-clock based, with a bounded LRU of buckets so a flood of distinct principals cannot grow memory without limit.

Keyed by **principal** wherever a token is present, falling back to client IP only for pre-authentication routes. This is the decision that keeps a legitimate stampede from being throttled: 20,000 distinct buyers behind one load generator share an IP but are distinct principals. An IP-keyed limiter on the reserve path would throttle the very burst the service exists to handle.

### Route classes

Every ceiling is a config value, changeable by environment variable with no code change and no redeploy.

| Class | Keyed by | Intent | Default |
|---|---|---|---|
| `reserve` | principal | Generous — must never throttle a legitimate on-sale rush | 120 / 10s |
| `read` | principal | Generous | 300 / 10s |
| `auth` | IP + email | Tight — this is the brute-force surface | 10 / 60s |
| `guest` | IP | Moderate — guest-row flooding | 60 / 60s |
| `admin` | principal | Tight — show creation is expensive | 30 / 60s |
| exempt | — | `/healthz`, `/readyz`, `/metrics` | no limit |

The reserve ceiling is set well above any legitimate single-principal burst. A principal who exceeds it is either misbehaving or retrying pathologically, and the per-user seat limit — a domain rule, not a transport rule — is what actually bounds how many seats one principal can take. The limiter protects the service; the domain rule protects fairness. Conflating the two is how a correctness grade turns into a wall of 429s.

### Response

```
HTTP 429
Retry-After: 3
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1730000003
```
with the standard error envelope and code `RATE_LIMITED`. Counted as `rate_limited_total{route_class}` so a throttling incident is visible rather than inferred from client complaints.

### Known limitation

Buckets are **per instance**. With N instances behind a load balancer the effective ceiling is N × the configured value. Accepted deliberately: the limiter is abuse protection, not a quota system, and correctness never depends on it. Making it exact would require Redis — a new dependency, a new failure mode, and a network round trip on the hot path, in exchange for precision nothing needs. Recorded as ADR-007 and RISK-003. If an exact global quota is ever required, the token bucket interface is the seam to swap.

### Burst escape hatch

A configured flag disables rate limiting entirely, and individual ceilings are env-tunable. This exists so a load test can isolate the claim path from transport policy without a redeploy. It is off by default and its state is logged at startup and exposed on `/readyz`, so a service running unlimited can never do so unnoticed.

---

## AuditMiddleware

Builds one record per request and enqueues it without blocking:

```python
record = AuditRecord(request_id=…, route=…, status_code=…, duration_ms=…,
                     user_id=…, outcome_code=…, show_id=…, seat_labels=…)
try:
    queue.put_nowait(record)
except QueueFull:
    metrics.audit_dropped.inc()
```

The request path never awaits a database write and never awaits queue capacity. `put_nowait` either succeeds or drops, and a drop is counted. The alternative — awaiting capacity — makes audit a source of backpressure on bookings, which inverts the priority: losing an audit row is an inconvenience, failing a booking is a defect.

A bounded queue drained in batches by `workers/audit_writer.py` on its own connection, outside any request transaction. Full design, including the drop policy and shutdown flush, in [10-observability.md](10-observability.md).

Fields are extracted from the request scope and the context vars. The middleware does not read the request body — buffering a body to audit it would double memory per in-flight request; `show_id` comes from path parameters and `seat_labels` from a context var set by the service that already parsed them.

---

## Rules for new middleware

- It may not perform a database query on the request path. Readiness is a route, not middleware.
- It may not block on anything unbounded. Every wait has a timeout.
- It must be safe to run on a request that fails before the route resolves.
- It must not consume the request body.
- It must reset any `ContextVar` it sets, in a `finally`.
- It goes in the chain where its dependencies are already established, and this document's order table is updated in the same commit.
