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
NotFoundError       404   SHOW_NOT_FOUND, SEAT_NOT_FOUND, RESERVATION_NOT_FOUND
ConflictError       409   SEAT_TAKEN, PER_USER_LIMIT, EMAIL_TAKEN,
                          IDEMPOTENCY_KEY_REUSED, IDEMPOTENCY_IN_PROGRESS,
                          SHOW_NOT_ON_SALE, RESERVATION_EXPIRED,
                          RESERVATION_CANCELLED, RESERVATION_CONFIRMED,
                          ALREADY_REGISTERED
ValidationError     422   VALIDATION_ERROR
RateLimitError      429   RATE_LIMITED
DependencyError     503   DATABASE_UNAVAILABLE, NOT_READY
InternalError       500   INTERNAL_ERROR
```

Services raise these. Nothing else. `HTTPException` is not raised in application code — it bypasses the envelope and the code registry, so a client would receive two different error shapes depending on which layer failed.

`core/error_codes.py` is the single registry: code → status, default message, whether it is a client-visible decline or an internal fault. Every code is listed there and nowhere else, so the set of possible error codes is enumerable from one file — which is what makes the client contract in [06-apis.md](06-apis.md) checkable against the code.

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
- `request_id` lets a user quote one string that locates every log line and the audit row for the request.

FastAPI's default validation error shape is replaced so that a 422 looks like every other failure, with the field errors under `details.fields`.

---

## Handlers

Registered in `main.py`, most specific first:

| Handler | Behaviour |
|---|---|
| `AppError` | Render the envelope from the exception. Log at the exception's own `log_level`. Increment the decline counter labelled by code. |
| `RequestValidationError` | Translate to the envelope with `code=VALIDATION_ERROR`, field errors in `details.fields`. Log at `info`. |
| `Exception` (catch-all) | Log at `error` **with a stack trace**, increment `unhandled_exceptions_total`, return 500 `INTERNAL_ERROR` with a generic message. Never leak `str(exc)`. |

A 500 reaching a client is a bug report about this service, not information for the caller. The stack goes to the logs, keyed by the request id the client already holds.

---

## Driver-error translation

Database exceptions are translated at the **repository boundary**. A raw `asyncpg.UniqueViolationError` reaching a route is a layering defect, because the route cannot know which constraint fired or what it means.

| Database condition | Translated to | Log level |
|---|---|---|
| `uq_users_email` violation | 409 `EMAIL_TAKEN` | info |
| `uq_idem_user_key` violation | handled by the idempotency flow, not an error | — |
| `uq_seat_active_claim` violation | 409 `SEAT_TAKEN` | **error** |
| `lock_not_available` (`55P03`) | 409 `SEAT_TAKEN`, metric label `lock_timeout` | warning |
| `deadlock_detected` (`40P01`) | 409 `SEAT_TAKEN`, metric label `deadlock` | **error** |
| `serialization_failure` (`40001`) | retried once in-request, then 409 | warning |
| Connection failure / pool timeout | 503 `DATABASE_UNAVAILABLE` | error |
| `query_canceled` (statement timeout) | 503 `DATABASE_UNAVAILABLE` | error |

Two of these deserve emphasis:

**`uq_seat_active_claim` is logged at `error` even though the client gets a clean 409.** That index is the backstop behind the guarded `UPDATE`; if it ever fires, the primary mechanism has a bug. The client still gets the correct answer, and we get paged.

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

```
http_request                  one per request, from the access log
seat_claim_attempt            before the atomic claim, with labels and principal
seat_claim_confirmed          a winner
seat_claim_declined           a loser, with outcome_code and conflicts
per_user_limit_declined       with limit and currently_held
idempotency_key_claimed       this request owns the key
idempotency_replay            a stored response returned
idempotency_key_reuse         same key, different fingerprint
idempotency_in_progress       bounded wait exhausted
idempotency_key_reclaimed     a stale in_progress row taken over
reservation_confirmed
reservation_cancelled
hold_sweep_batch              count swept, duration
audit_flush_batch             count written, duration
audit_queue_saturated         drop occurred, with queue depth
rate_limited                  with route class and key kind
readiness_check_failed        with the failing dependency
startup / shutdown            with resolved config summary, secrets redacted
unhandled_exception           always with a stack trace
```

### Level discipline

| Level | Means | Examples |
|---|---|---|
| `info` | Expected, including every domain decline | `seat_claim_declined`, `idempotency_replay`, `rate_limited` |
| `warning` | Unexpected but handled | lock timeout, serialization retry, audit drop, stale key reclaim |
| `error` | A fault in this service | unhandled exception, backstop index violation, deadlock, readiness failure |

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
4. The `request_id` column of every row the request wrote, and of its audit row.

So a single value answers, for any production incident: what the client was told, what the service logged, and what it wrote to the database. That is the whole purpose of the context var in [07-middleware.md](07-middleware.md).
