# Architecture

## Shape

A single stateless FastAPI process in front of one PostgreSQL primary. No cache, no queue, no second datastore. The atomic decision lives in the database, which is the only component that can serialize contending writers correctly without a distributed protocol.

```
          client
            │  X-Request-ID, Authorization: Bearer …
            ▼
   ┌─────────────────────────────┐
   │  middleware chain            │  request ctx → access log → metrics
   │                              │  → rate limit → audit enqueue
   └─────────────┬───────────────┘
                 ▼
   ┌─────────────────────────────┐
   │  api/routes  +  api/deps     │  HTTP shape, auth via Depends, RBAC
   └─────────────┬───────────────┘
                 ▼
   ┌─────────────────────────────┐
   │  services                    │  orchestration, transaction boundary
   │      └── domain              │  pure rules, no IO
   └─────────────┬───────────────┘
                 ▼
   ┌─────────────────────────────┐
   │  repositories                │  all SQL, driver-error translation
   └─────────────┬───────────────┘
                 ▼
            PostgreSQL

   workers:  hold_sweeper        (expires lapsed holds)
             audit_writer        (drains the audit queue in batches)
```

## Layering

| Layer | Responsibility | Forbidden |
|---|---|---|
| `api/routes` | Parse, delegate to one service call, serialize, set status | Any SQL, any branching on business state, any helper logic |
| `api/deps` | `Depends` providers: principal, RBAC, session, idempotency context | Business decisions |
| `services` | Orchestration, transaction boundaries, error mapping | Raw SQL, HTTP concepts |
| `domain` | Pure business rules and value objects | Any IO whatsoever |
| `repositories` | Every statement, driver-error translation, `request_id` stamping | Transaction control, business rules |
| `helpers` | Project-aware reusable logic — seat labels, pricing, pagination | IO, framework imports |
| `utils` | Pure project-agnostic functions — hashing, canonical JSON, time, backoff | Any project knowledge |
| `core` | Config, errors, logging, context, security, metrics, constants | Anything domain-specific |
| `middleware` | Cross-cutting ASGI concerns | Business decisions |
| `workers` | Background loops | Being required for correctness of a claim |

Dependencies point one way: `routes → services → repositories → db`. `domain` imports nothing from the project. A repository importing a service is a layering defect; so is a route importing a repository.

## Module layout

```
app/
  main.py                    app factory, lifespan, router + middleware registration
  core/
    config.py                pydantic-settings; every tunable, env-backed
    constants.py             enums: SeatStatus, ReservationStatus, Role, EventKind, headers
    context.py               ContextVars: request_id, principal
    errors.py                AppError hierarchy
    error_codes.py           the code registry
    logging.py               JSON logger factory, context binding
    security.py              JWT encode/decode, Argon2 hashing
    metrics.py               Prometheus collectors, declared once
  db/
    engine.py                asyncpg pool lifecycle, statement/lock timeouts
    session.py               connection + transaction dependencies
    sql.py                   shared SQL fragments (effective-status expression)
    migrations/              alembic
  middleware/
    request_context.py  access_log.py  metrics.py  rate_limit.py  audit.py
  api/
    deps.py                  get_current_user, require_admin, require_role, idempotency
    routes/
      __init__.py            router assembly
      health.py  auth.py  shows.py  reservations.py  admin.py  metrics.py
  schemas/
    auth.py  shows.py  reservations.py  common.py
  domain/
    models.py                dataclasses
    policies.py              limit, TTL, pricing, label rules
  services/
    auth_service.py  show_service.py  reservation_service.py
    idempotency_service.py  audit_service.py
  repositories/
    base.py  user_repo.py  show_repo.py  seat_repo.py
    reservation_repo.py  idempotency_repo.py  audit_repo.py
  helpers/
    seat_labels.py  pricing.py  pagination.py  time_windows.py
  utils/
    ids.py  canonical_json.py  hashing.py  backoff.py  clock.py
  workers/
    hold_sweeper.py  audit_writer.py
```

`seat_repo.py` holds the atomic claim. It is the single most important file in the service and the only place seat state transitions are expressed.

## Request lifecycle

1. **Request context** — accept or mint a UUID request id, bind it to a `ContextVar` and the logger, echo it on the response.
2. **Access log** — capture method, path, duration, status on the way out.
3. **Metrics** — latency histogram, in-flight gauge.
4. **Rate limit** — per-principal bucket by route class; a 429 is still traceable because the id already exists.
5. **Audit enqueue** — non-blocking `put_nowait` onto a bounded queue.
6. **Route** — `Depends` resolves the principal and role before the handler body runs.
7. **Service** — opens the transaction, calls domain rules, calls repositories, commits or rolls back.
8. **Response** — a response model, or an error envelope produced by an exception handler.

## Transaction boundaries

Services own them, exclusively. A repository method participates in a transaction it is handed; it never begins or commits one. A route never sees one.

The reserve path is two transactions, deliberately:

| Txn | Contents | Why separate |
|---|---|---|
| T1 | Insert the idempotency key as `in_progress` | Must commit before any claim so the unique constraint publishes ownership to concurrent duplicates |
| T2 | Lock quota row → check limit → claim seats → insert reservation and seat links → mark the key `completed` with the stored response | Result and key completion commit atomically, so a crash can never leave a reservation whose key says `in_progress` |

If T2 rolls back, the key remains `in_progress` and is reclaimable after a staleness window. If T2 commits, the response is durably recorded for replay. There is no ordering in which a reservation exists without its key being complete.

## Generic event model

Cinema and concert differ only in data:

| Variable | Carried by |
|---|---|
| Event kind | `events.kind`, free-form, validated against config |
| Layout | `seats.section`, `row_label`, `seat_number`, all nullable; `label` is the canonical identity |
| Pricing | `shows.price_paise` with per-seat `seats.price_paise` override |
| Booking limit | `shows.per_user_limit` |
| Hold duration | `shows.hold_ttl_seconds` |
| Sale window | `shows.sales_open_at`, `sales_close_at`, `status` |

No code branches on event kind. A reserved-seating concert and a screening traverse identical statements. Unassigned-seating general admission is explicitly **out of scope** — it is a capacity-counter problem, not a unique-seat problem, and would need a different atomic mechanism.

## Failure posture

- A dependency failure fails closed: readiness reports unready, requests return a 503 with a code rather than a stack trace.
- The claim path is bounded by `lock_timeout` and `statement_timeout`; exhausting either is translated to a 409, never a 500.
- Workers are not in the correctness path. Expiry is enforced lazily inside the claim predicate, so a dead sweeper degrades reporting freshness, not safety. A dead audit writer loses audit records and raises a metric; it cannot stall a request.
- The connection pool is sized against the database's connection ceiling, not against expected concurrency; queueing at the pool is preferable to refusal at the database.

## What is deliberately absent

| Not built | Why |
|---|---|
| Redis | Nothing needs it. Rate limiting is per-instance by choice; the atomic decision belongs in the database. Adding it would add a failure mode and no correctness. |
| Message broker | Audit is the only async path and an in-process bounded queue with a drop policy is the right size for it. |
| Read replica | Reads are cheap and must be consistent with claims; a replica would introduce lag visible as an invariant violation. |
| Payment integration | `amount_paise` is computed and recorded; capture is out of scope and the hold/confirm split is where it would attach. |
| UI | Out of scope. The monitoring view over the audit table is a possible later stage, read-only, and not on the request path. |
