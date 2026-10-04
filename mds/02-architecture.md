# Architecture

## Shape

A single stateless FastAPI process in front of one PostgreSQL primary. No cache, no queue, no second datastore. The atomic decision lives in the database, which is the only component that can serialize contending writers correctly without a distributed protocol.

```
          client
            │  X-Request-ID, Authorization: Bearer …
            ▼
   ┌─────────────────────────────┐
   │  middleware chain            │  request context → access log
   │                              │  → rate limit → audit
   └─────────────┬───────────────┘
                 ▼
   ┌─────────────────────────────┐
   │  api/routes  +  api/deps     │  HTTP shape, auth via Depends, RBAC
   └─────────────┬───────────────┘
                 ▼
   ┌─────────────────────────────┐
   │  services                    │  orchestration, transaction boundary
   └─────────────┬───────────────┘
                 ▼
   ┌─────────────────────────────┐
   │  repositories                │  all SQL, driver-error translation
   └─────────────┬───────────────┘
                 ▼
            PostgreSQL
```

**One background task, and it is not near seat state.** The audit writer drains a buffer on its own connection. Expiry is enforced entirely by the claim predicate (ADR-017), and the availability gauge is computed when `/metrics` is scraped. The only thread besides the event loop's own pools is the log writer.

## Layering

| Layer | Responsibility | Forbidden |
|---|---|---|
| `api/routes` | Parse, delegate to one service call, serialize, set status | Any SQL, any branching on business state, any helper logic |
| `api/deps` | `Depends` providers: principal, RBAC, session, idempotency context | Business decisions |
| `services` | Orchestration, transaction boundaries, error mapping | Raw SQL, HTTP concepts |
| `domain` | The dataclasses passed between layers | Any IO whatsoever |
| `repositories` | Every statement, driver-error translation, `request_id` stamping | Transaction control, business rules |
| `helpers` | Project-aware reusable logic — keyset pagination | IO, framework imports |
| `utils` | Pure project-agnostic functions — canonical JSON, the cursor codec | Any project knowledge |
| `core` | Config, errors, logging, context, security, metrics, constants | Anything domain-specific |
| `middleware` | Cross-cutting ASGI concerns | Business decisions |

Dependencies point one way: `routes → services → repositories → db`. `domain` imports nothing from the project. A repository importing a service is a layering defect; so is a route importing a repository.

## Module layout

```
app/
  main.py                    app factory, lifespan (pool, admin bootstrap),
                             middleware and exception-handler registration
  alembic.ini                inside app/, because the image copies app/ only
  core/
    config.py                pydantic-settings; every tunable, env-backed,
                             with relationship checks at startup
    constants.py             enums and named values: statuses, roles, headers,
                             route classes, log events — deliberately no
                             EventKind: the permitted set is configuration (ADR-025)
    context.py               ContextVars: request id, outcome code
    errors.py                AppError hierarchy
    error_codes.py           the code registry
    logging.py               JSON formatter, redaction, queue-backed writer
    security.py              Argon2 in a bounded pool; JWT issue and verify
    metrics.py               Prometheus collectors, declared once
  db/
    engine.py                the asyncpg pool; session guards as startup parameters
    session.py               acquire() and transaction(); driver-unavailable → 503
    sql.py                   the effective-status fragments, each defined once
    migrations/              Alembic env and the single revision
  middleware/
    request_context.py  access_log.py  rate_limit.py  audit.py
  api/
    deps.py                  get_current_user, require_admin
    routes/
      __init__.py            router assembly
      health.py  auth.py  shows.py  reservations.py  metrics.py
      admin.py  pages.py
  schemas/
    common.py  auth.py  shows.py  reservations.py
  domain/
    models.py                dataclasses
  services/
    health_service.py  auth_service.py  show_service.py
    reservation_service.py  metrics_service.py
    audit_service.py  admin_service.py
  repositories/
    base.py  health_repo.py  user_repo.py  show_repo.py  seat_repo.py
    reservation_repo.py  idempotency_repo.py  audit_repo.py
  helpers/
    pagination.py
  utils/
    canonical_json.py  cursor.py
```

`seat_repo.py` holds the atomic claim. It is the single most important file in the service and the only place seat state transitions are expressed.

The idempotency decision tree lives in `reservation_service.py` rather than a service of its own: reserve is its only caller, and the two transactions it spans are easier to read side by side.

## Request lifecycle

1. **Request context** — accept or mint a UUID request id, bind it to a `ContextVar` and the ASGI scope, echo it on the response.
2. **Access log** — method, path, route template, duration, status and outcome code, on the way out.
3. **Rate limit** — a bucket per identity and route class; a 429 is still traceable because the id already exists.
4. **Audit** — on the way out, one record into a buffer that never blocks.
5. **Route** — `Depends` resolves the principal and role before the handler body runs.
6. **Service** — opens the transaction, calls repositories, commits or rolls back.
7. **Response** — a response model, or the error envelope from an exception handler.

## Transaction boundaries

Services own them, exclusively. A repository method participates in a transaction it is handed; it never begins or commits one. A route never sees one.

The reserve path is two transactions, deliberately:

| Txn | Contents | Why separate |
|---|---|---|
| T1 | Insert the idempotency key as `in_progress` | Must commit before any claim so the unique constraint publishes ownership to concurrent duplicates |
| T2 | Lock the key row by id (ownership, ADR-033) → lock quota row → check limit → claim seats → **close superseded claim rows** → insert reservation and seat links → mark the key `completed` with the stored response | Result and key completion commit atomically, so a crash can never leave a reservation whose key says `in_progress` |

The superseded-row closure is inside T2 and after the claim, not before and not folded into the insert's statement; the ordering argument is ADR-019.

If T2 commits, the response is durably recorded for replay. If T2 rolls back — for a domain decline or a fault alike — the key row is **deleted** in a small follow-up transaction so a retry genuinely re-attempts (ADR-020). If that delete also fails, the key stays `in_progress` and the staleness reclaim handles it. There is no ordering in which a reservation exists without its key being complete.

Lock order across the whole service is three tiers and is proved deadlock-free in [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md): a claim takes its own idempotency key row → quota → seats (ascending label) → `reservation_seats`; a confirm or cancel takes its own `reservations` row → seats (ascending label) → `reservation_seats`.

## Generic event model

Cinema and concert differ only in data:

| Variable | Carried by |
|---|---|
| Event kind | `shows.event_kind`, free-form `TEXT`, validated against `ALLOWED_EVENT_KINDS` from configuration — never a Python or database enum (ADR-025) |
| Layout | `seats.section`, nullable; `label` is the canonical identity and the only one stored |
| Pricing | `shows.price_paise` with per-seat `seats.price_paise` override |
| Booking limit | `shows.per_user_limit` |
| Hold duration | `shows.hold_ttl_seconds` |
| On sale or not | `shows.status`. The `sales_open_at` / `sales_close_at` columns exist and are not yet read (future scope) |

No code branches on event kind. A reserved-seating concert and a screening traverse identical statements. Unassigned-seating general admission is explicitly **out of scope** — it is a capacity-counter problem, not a unique-seat problem, and would need a different atomic mechanism.

## Failure posture

- A dependency failure fails closed: readiness reports unready, requests return a 503 with a code rather than a stack trace.
- The claim path is bounded by `lock_timeout` and `statement_timeout`, which mean different things and answer differently: exhausting `lock_timeout` is contention and is a 409 `SEAT_TAKEN`; exhausting `statement_timeout` is a fault and is a 503 `DATABASE_UNAVAILABLE`. `statement_timeout` is configured above `lock_timeout` by a validated margin, so on the claim path the lock timeout always fires first and a 503 there is unambiguous evidence of a genuine fault (ADR-027).
- There is no worker in the correctness path because there is no worker. Expiry is enforced entirely inside the claim predicate (ADR-017).
- The connection pool is sized against the database's connection ceiling, not against expected concurrency; queueing at the pool is preferable to refusal at the database.

## What is deliberately absent

| Not built | Why |
|---|---|
| Redis | Nothing needs it. Rate limiting is per-instance by choice; the atomic decision belongs in the database. Adding it would add a failure mode and no correctness. |
| Message broker | Nothing is asynchronous. The audit trail, if built, is an in-process bounded queue with a drop policy. |
| Metrics middleware, exception boundary | Designed, not built: [17-future-scope.md](17-future-scope.md). |
| Read replica | Reads are cheap and must be consistent with claims; a replica would introduce lag visible as an invariant violation. |
| Payment integration | `amount_paise` is computed and recorded; capture is out of scope and the hold/confirm split is where it would attach. |
| UI | One static page served by this service; no build step, no second deployment ([18-frontend.md](18-frontend.md), ADR-036). |
