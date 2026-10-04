# Error handling and logging

One envelope for every failure, one code per outcome, one structured log line per event, every one of them correlated by request id.

The hard requirement behind this document: **a domain outcome is never a 5xx.** Losing a seat race is business as usual at 20,000 requests per second, and a service that returns 500 for it is reporting its own failure rather than the correct answer.

---

## The exception hierarchy

`core/errors.py`:

```
AppError(Exception)
    code: str                 stable machine identifier
    http_status: int
    message: str              safe for a client; never contains internals
    details: dict | None      structured, machine-readable context
    log_level: str            "info" for declines, "error" for faults
```

Subclasses, one per outcome, carrying their own status and code:

```
AuthError           401   UNAUTHENTICATED, INVALID_CREDENTIALS
PermissionError     403   FORBIDDEN
NotFoundError       404   SHOW_NOT_FOUND, SEAT_NOT_FOUND, RESERVATION_NOT_FOUND,
                          ROUTE_NOT_FOUND
MethodError         405   METHOD_NOT_ALLOWED
ConflictError       409   SEAT_TAKEN, PER_USER_LIMIT, EMAIL_TAKEN,
                          IDEMPOTENCY_KEY_REUSED, IDEMPOTENCY_IN_PROGRESS,
                          SHOW_NOT_ON_SALE, RESERVATION_EXPIRED,
                          RESERVATION_CANCELLED,
                          ALREADY_REGISTERED
ValidationError     422   VALIDATION_ERROR
RateLimitError      429   RATE_LIMITED
DependencyError     503   DATABASE_UNAVAILABLE, NOT_READY
InternalError       500   INTERNAL_ERROR
```

Services raise these. Nothing else. `HTTPException` is not raised in application code — it bypasses the envelope and the code registry, so a client would receive two different error shapes depending on which layer failed.

`core/error_codes.py` is the single registry: code → status, default message, whether it is a client-visible decline or an internal fault. Every code is listed there and nowhere else, so the set of possible error codes is enumerable from one file — which is what makes the client contract in [06-apis.md](06-apis.md) checkable against the code.

### Operational codes

Five codes appear in no endpoint contract table, because they are not outcomes of any particular endpoint. They are enumerated **here**, and this list plus the `06-apis.md` contract tables is the closed set:

| Code | Status | Raised by |
|---|---|---|
| `ROUTE_NOT_FOUND` | 404 | the router, for a path that matches nothing (ADR-023) |
| `METHOD_NOT_ALLOWED` | 405 | the router, for a known path with an unsupported method; the `Allow` header is preserved |
| `DATABASE_UNAVAILABLE` | 503 | a connection failure, a pool-acquire timeout, or a statement cancelled by `statement_timeout` |
| `NOT_READY` | 503 | `/readyz` failing closed |
| `INTERNAL_ERROR` | 500 | the catch-all, for a fault with no registered code |

The registry assertion is therefore an **equality**: the registry's code set equals the `06-apis.md` contract codes union this table. Asserting equality against the contract codes alone is unsatisfiable, because these five would always be surplus; asserting only a subset relation would let a stray code be added unnoticed.

---

## Response envelope

Every failure, including 422 and 429:

```json
{
  "error": {
    "code": "SEAT_TAKEN",
    "message": "One or more seats are no longer available",
    "details": { "requested": ["A12", "A13"], "conflicts": ["A13"] },
    "request_id": "7f1c…"
  }
}
```

- `code` is the contract. Clients branch on it, never on `message`.
- `message` is human-readable and safe. No SQL, no stack frame, no internal identifier, no table name.
- `details` is structured and specific: which seats conflicted, what the limit was, how many are currently held. A decline the client cannot act on is a worse decline.
- `request_id` lets a user quote one string that locates every log line for the request and every row it wrote.

FastAPI's default validation error shape is replaced so that a 422 looks like every other failure, with the field errors under `details.fields`.

---

## Handlers

Registered in `main.py`, most specific first:

| Handler | Behaviour |
|---|---|
| `AppError` | Render the envelope from the exception, with any headers it carries (`Retry-After`, `Allow`). Log `app_error` at the exception's own `log_level`, with `outcome_code`. The reserve decline counter is incremented by the service, not here. |
| `RequestValidationError` | Translate to the envelope with `code=VALIDATION_ERROR`, field errors in `details.fields`. Log `validation_failed` at `info`. |
| `StarletteHTTPException` | 404 → `ROUTE_NOT_FOUND`, 405 → `METHOD_NOT_ALLOWED` with the router's `Allow` header preserved; both in the standard envelope, logged at `info`. Any other status logs `unhandled_http_exception` at `error` and answers 500 `INTERNAL_ERROR` — see below. |
| `Exception` (retained as a second line of defence) | Log at `error` **with a stack trace**, increment `unhandled_exceptions_total`, return 500 `INTERNAL_ERROR` with a generic message. Never leak `str(exc)`. |

A 500 reaching a client is a bug report about this service, not information for the caller. The stack goes to the logs, keyed by the request id the client already holds.

**Why a non-404/405 `StarletteHTTPException` is a fault.** The only legitimate sources of that exception here are the router's own 404 and 405. Anything else means application code raised `HTTPException`, which these conventions forbid precisely because it bypasses the envelope and the registry. Answering 500 and logging at `error` is how that violation gets noticed, rather than quietly serving a status with no registered code.

**Known gap — the catch-all is meant to be a middleware boundary too, and is not yet** ([17-future-scope.md](17-future-scope.md), item 5). What follows is the design; today an unhandled exception is logged twice, the second time without its request id.

**The design:** Registering a handler for `Exception` in Starlette installs it on `ServerErrorMiddleware`, which builds the response and then **re-raises unconditionally** so the ASGI server logs the failure (LEARN-010). That second line is emitted outside every user middleware, after the request-id ContextVar has been reset, so it cannot carry a request id — directly contradicting "every log line carries `request_id`". `ExceptionBoundaryMiddleware`, registered immediately inside `RequestContextMiddleware`, therefore catches, logs once with the id and a stack, and **returns** the envelope, so nothing escapes to be re-raised (ADR-024). The `Exception` handler stays registered to cover a failure in the request-context middleware itself — the one region no boundary inside it can reach, and one where double-logging is appropriate.

---

## Driver-error translation

Database exceptions are translated at the **repository boundary**. A raw `asyncpg.UniqueViolationError` reaching a route is a layering defect, because the route cannot know which constraint fired or what it means.

| Database condition | Translated to | Log level |
|---|---|---|
| `uq_users_email` violation | 409 `EMAIL_TAKEN` | info |
| `uq_idem_user_key` violation | handled by the idempotency flow, not an error | — |
| `uq_seat_active_claim` violation | 409 `SEAT_TAKEN` | **error** |
| `lock_not_available` (`55P03`) | 409 `SEAT_TAKEN`, metric label `lock_timeout` — on every statement of the reserve, confirm and cancel paths that can wait on a row lock (LEARN-017) | info |
| a value the database cannot store (`DataError`, e.g. a NUL character) | 422 `VALIDATION_ERROR`, as a backstop behind the schemas | info |
| foreign-key violation on the idempotency key insert | 401 `UNAUTHENTICATED`: a signed token whose subject has no user row | info |
| `deadlock_detected` (`40P01`) | 409 `SEAT_TAKEN`, metric label `deadlock` | **error** |
| `serialization_failure` (`40001`) | not handled: nothing runs above `READ COMMITTED`, so it cannot currently occur (future scope) | — |
| Connection failure / pool timeout | 503 `DATABASE_UNAVAILABLE` | error |
| `query_canceled` (`57014`, statement timeout) | 503 `DATABASE_UNAVAILABLE` | **error** |

Three of these deserve emphasis:

**`lock_timeout` is a decline and `statement_timeout` is a fault**, and they must not be collapsed (ADR-027). A lock timeout means another transaction holds the row — a contention outcome. A statement timeout means a statement could not finish in the time the database was given, which is a wrong plan, an overloaded database, or something pathological. Reporting the second as a 409 would tell a client a seat is taken when it may be free, and hide the fault. The distinction is only trustworthy because `DB_LOCK_TIMEOUT_MS` is held strictly below `DB_STATEMENT_TIMEOUT_MS` by a configured margin, validated at startup: reversed, every hot-seat decline would arrive as a 503.

**`uq_seat_active_claim` is logged at `error` even though the client gets a clean 409.** That index is the backstop behind the guarded `UPDATE`; if it ever fires, the primary mechanism has a bug. Since ADR-019 closes superseded claim rows inside the claim transaction, there is no legitimate path that produces this violation — a lapsed hold's open row no longer collides with its successor — so the alert has no false-positive source. The client still gets the correct answer, and we get paged.

**`deadlock_detected` is logged at `error`.** The lock-ordering argument in [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md) says a deadlock is impossible. One occurring means the argument is wrong or a new code path violates the order. The client is shielded; the alert fires.

---

## Log schema

Single-line JSON on stdout. The platform ships it.

Required on every line:

| Field | Notes |
|---|---|
| `ts` | RFC 3339 UTC, microsecond precision |
| `level` | `debug` / `info` / `warning` / `error` |
| `event` | stable snake_case identifier, **not a sentence** |
| `request_id` | from the context var; `null` only for pre-context startup lines |
| `service`, `version` | for multi-service log aggregation |

Added where applicable: `user_id`, `is_guest`, `show_id`, `seat_labels`, `reservation_id`, `idempotency_key`, `outcome_code`, `route`, `status`, `duration_ms`, `worker`, `batch_size`.

`event` is an identifier because log lines are queried, not read. `seat_claim_declined` can be counted and alerted on; "Could not reserve seat A12 for user …" cannot.

### Event catalogue

The events the code emits today:

```
startup / shutdown            with the resolved config summary, secrets redacted
http_request                  one per request, from the access log
app_error                     any AppError rendered, with outcome_code and status
validation_failed             a 422, with the offending field paths
unhandled_exception           always with a stack trace
unhandled_http_exception      a framework HTTPException that is neither 404 nor 405
reservation_created           a winner: user, show, reservation, labels, outcome
reservation_confirmed
reservation_cancelled
claim_backstop_violated       uq_seat_active_claim fired — an alert
claim_deadlock                the lock order was broken — an alert
idempotency_release_failed    a key could not be released after a rolled-back T2
readiness_check_failed        with the failing dependency
metrics_gauge_unavailable     /metrics could not read the gauge from the database
admin_bootstrapped / admin_bootstrap_skipped
```

`app_error` and `validation_failed` are the generic lines every handler emits, carrying the specific code as a field rather than in the event name — the code is what gets queried and counted, and a per-code event name would make the catalogue grow with the registry. A decline has no line of its own beyond those two.

There is no line for a hold lapsing or a superseded claim row being closed: the first is never observed, and the second would fire on a large share of claims during a burst. `superseded_claims_closed_total` carries that signal.

### Level discipline

| Level | Means | Examples |
|---|---|---|
| `info` | Expected, including every domain decline | `app_error` for a 409, `validation_failed`, `http_request` |
| `warning` | Unexpected but handled | `readiness_check_failed`, `metrics_gauge_unavailable`, `admin_bootstrapped` |
| `error` | A fault in this service | unhandled exception, backstop index violation, deadlock, statement timeout, readiness failure, an `HTTPException` with an unexpected status |

**A decline is `info`.** 20,000 losers of a seat race are not 20,000 errors; logging them at `error` makes the error rate meaningless and buries the one line that matters. This is the most important line in this document.

Every `error` line carries a stack trace. An `error` with no stack is unactionable at 2am.

### Never logged

Passwords, password hashes, tokens, Authorization headers, the signing secret, the database URL with credentials, full request bodies. Email addresses are logged only where an auth event requires them, never alongside a credential.

A redaction filter is applied by the logger factory as a defence against a future careless `extra=`, but the primary rule is not to pass the value in the first place.

### Volume

A 20,000-request burst at three lines per request is 60,000 lines. Mitigations: health-check paths excluded, `seat_claim_attempt` is `debug` and off by default in production, and declines carry the minimum field set. The access log line and the outcome line are the two that must survive at full volume.

---

## Correlation

One request id reaches four places:

1. Every log line for the request.
2. The `X-Request-ID` response header.
3. The `request_id` field in the error envelope.
4. The `request_id` column of every row the request wrote.

So a single value answers, for any production incident: what the client was told, what the service logged, and what it wrote to the database. That is the whole purpose of the context var in [07-middleware.md](07-middleware.md).
