# Middleware

Three ASGI middlewares, all pure ASGI rather than `BaseHTTPMiddleware`: the latter runs the application in a separate task, so a `ContextVar` set inside the application — the outcome code the access log reports — would be invisible to the layers wrapping it.

## Order

```
request
  → RequestContextMiddleware     request id: adopt or mint, bind, echo
  → AccessLogMiddleware          one line per request, on the way out
  → RateLimitMiddleware          per-identity bucket by route class
  → router → Depends → handler
```

Registration in `main.py` is the reverse of this, because the last middleware added is the outermost.

Why this order:

- **Request context is first**, so every later layer — including a 429 — has a request id.
- **The access log wraps the rate limiter**, so a throttled request still produces its line and its status. A limiter that hides its own rejections makes a throttling incident invisible.
- **The rate limiter is innermost**, so a rejected request costs a bucket check and nothing else: no routing, no dependency resolution, no database connection.

Domain error handling is **not** middleware. FastAPI exception handlers render the envelope for `AppError`, `RequestValidationError`, `StarletteHTTPException` and bare `Exception` ([08-error-logging.md](08-error-logging.md)), because they have the resolved route and the raised exception.

Not built, and described in [17-future-scope.md](17-future-scope.md): a metrics middleware, an audit middleware, and an exception-boundary middleware.

---

## RequestContextMiddleware

```
inbound X-Request-ID present and parses as UUID  →  adopt it
otherwise                                        →  mint uuid4
bind to the ContextVar and to the ASGI scope
→ call downstream
set response header X-Request-ID
reset the ContextVar
```

- An inbound id is adopted only if it is a UUID. A malformed value is replaced rather than rejected: it would end up in a `uuid` column and in log fields, and a client's bad header is not worth failing a booking over.
- Adopting a client's id lets a caller correlate across its own retries.
- The id is read from the `ContextVar` by the logger and by the repository layer, which stamps it on every row it writes. Nothing passes it through a function signature.
- It is **also** carried on the ASGI scope, because Starlette's `ServerErrorMiddleware` runs outside this middleware, after the `ContextVar` has been reset; the catch-all handler reads it from there.

## AccessLogMiddleware

One structured line per request, emitted on the way out:

```json
{"ts":"…","level":"info","event":"http_request","request_id":"…",
 "method":"POST","path":"/shows/…/reserve","route":"/shows/{show_id}/reserve",
 "status":409,"duration_ms":7.1,"outcome_code":"SEAT_TAKEN"}
```

`route` is the path template, not the concrete path, so a burst against thousands of show ids groups into one series; an unmatched path is logged under the single label `unmatched`. The concrete `path` is kept for lookup. `/healthz`, `/readyz` and `/metrics` are excluded — a platform probe every few seconds would otherwise dominate the volume.

## RateLimitMiddleware

### Design

An in-process token bucket per `(identity, route class)` on a monotonic clock, held in a bounded least-recently-used map so a flood of distinct identities evicts idle buckets instead of growing memory.

**Keyed by principal wherever a valid token is present, and by client address only before authentication.** This is the decision that keeps an on-sale rush from being throttled: twenty thousand buyers behind one address are twenty thousand principals. The token is *verified*, not merely decoded — an unverified subject would let a client name a fresh bucket on every request.

The route class is decided from the method and path alone, before routing:

| Class | Applies to | Keyed by | Default |
|---|---|---|---|
| `guest` | `POST /auth/guest` | client address | 60 / 60s |
| `auth` | other `POST /auth/*` | client address | 10 / 60s |
| `admin` | `POST /shows` | principal | 30 / 60s |
| `reserve` | every other `POST` — reserve, confirm, cancel | principal | 120 / 10s |
| `read` | everything else | principal, or address if anonymous | 300 / 10s |
| exempt | `/healthz`, `/readyz`, `/metrics` | — | no limit |

`"120/10s"` is a bucket of 120 that refills over 10 seconds. Every ceiling is a `RATE_LIMIT_<CLASS>` environment variable, validated at startup.

**The `guest` ceiling is the one with a correctness consequence.** A guest principal costs nothing to create and each carries its own per-user seat limit, so the number of guests one client can mint is what bounds how many seats one client can take. The limiter narrows that; it does not close it (RISK-014, and item 1 of [17-future-scope.md](17-future-scope.md)).

The reserve ceiling is set well above any legitimate single-principal burst. The limiter protects the service; the per-user limit — a domain rule — protects fairness. Conflating the two is how a correctness grade turns into a wall of 429s.

### The client address

Taken from `X-Forwarded-For`, counting `RATE_LIMIT_TRUSTED_PROXY_HOPS` entries **from the right**. A client's own entries arrive on the left and each proxy appends on the right, so the leftmost entry is whatever the client chose to send; reading it would let any client pick its own bucket. `0` ignores the header and uses the socket peer.

The hop count is a property of the deployment and it must be right: too low and the limiter sees the platform's own internal address, shared by every client. A 429's `details.limited_by` names the address the limit was applied to, so a wrong value is visible from outside (LEARN-018).

### Response

```
HTTP 429
Retry-After: 3
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1730000003
```

with the standard envelope, code `RATE_LIMITED`, `details.route_class` and `details.limited_by`. Counted as `rate_limited_total{route_class}`.

### Known limitation

Buckets are **per process**. With N instances the effective ceiling is N times the configured value. Accepted: the limiter is abuse protection, not a quota, and no correctness property depends on it. An exact global limiter needs a shared store — a new dependency and a network round trip on the hot path (ADR-007, RISK-003).

### Switching it off

`RATE_LIMIT_ENABLED=false` disables limiting entirely, so a load test can isolate the claim path from transport policy. Its state is in the startup log line and reported by `/readyz` as `rate_limit_enabled`, so a service running unlimited cannot do so unnoticed. The test suite runs with it off, because every test client shares one address; `tests/integration/test_rate_limit.py` switches it on for its own tests.
