---
name: fastapi-conventions
description: Mandatory code structure and conventions for this FastAPI service — layering rules, where logic may and may not live, config/no-hardcoding policy, the error and logging contract, request-id propagation, middleware order, and dependency-based auth. Load before writing or reviewing any code under app/.
---

# FastAPI conventions

## Layering

```
api/routes      HTTP only: parse, delegate, serialize, status code
api/deps        Depends providers: auth, RBAC, db session, idempotency
services        orchestration + transaction boundaries
domain          pure business rules, zero IO
repositories    all SQL, one module per aggregate
helpers         project-aware reusable logic (seat labels, pricing, pagination)
utils           pure, project-agnostic functions (hashing, canonical json, time, backoff)
core            config, errors, logging, context, security, metrics, constants
middleware      cross-cutting ASGI concerns
workers         background tasks (hold sweeper, audit writer)
```

Dependencies point one direction only: `routes → services → repositories → db`. `domain` depends on nothing. A repository never imports a service. A route never imports a repository.

**No helper code in `api/routes/`.** A route body is: call a dependency, call one service method, return a response model. If a route needs to transform, validate beyond the schema, branch on business state, or build SQL, that code belongs in a service, helper, or repository. A route function longer than ~15 lines is a smell; one containing a loop over domain objects is a defect.

Routes are grouped by resource and audience, not dumped into one file: `health.py`, `auth.py`, `shows.py`, `reservations.py`, `admin.py`, `metrics.py`.

## Nothing hardcoded

Every tunable lives in `core/config.py` as a typed `pydantic-settings` field with an env var and a documented default: limits, TTLs, pool sizes, rate-limit buckets, timeouts, token lifetimes, batch sizes, sweeper intervals.

Every repeated literal lives in `core/constants.py` as an enum or named constant: seat statuses, reservation statuses, roles, event kinds, error codes, header names. A bare string literal for a status or role anywhere outside that module is a defect.

Magic numbers in SQL (page sizes, `LIMIT`, intervals) are bound parameters sourced from config, never inlined.

## Request id

`core/context.py` holds a `ContextVar` for the request id. `RequestContextMiddleware` accepts an inbound `X-Request-ID` when it parses as a UUID, otherwise mints one, sets the ContextVar, and echoes it on every response including error responses.

- Every log line carries `request_id` as a top-level field.
- Every mutable table carries a `request_id` column, populated by the repository layer from the ContextVar — never passed down through every function signature.
- Background workers set their own correlation id per batch so sweeper and audit writes are traceable too.

## Logging

One logger factory in `core/logging.py` emitting single-line JSON. Required fields: `ts`, `level`, `event`, `request_id`. Add `user_id`, `show_id`, `seat_labels`, `idempotency_key`, `outcome`, `duration_ms` where they apply.

- `event` is a stable snake_case identifier, not a sentence. Log lines are queried, not read.
- Never log secrets, tokens, password hashes, or full auth headers.
- Never use `print`. Never use the root logger directly.
- Domain declines log at `info` with an `outcome`. Only genuine faults log at `error`, and an `error` line always carries a stack trace.

## Errors

`core/errors.py` defines an `AppError` base carrying `code`, `http_status`, `message`, `details`. Every domain outcome is a subclass registered in `core/error_codes.py`. Exception handlers render one envelope for every failure:

```json
{"error": {"code": "SEAT_TAKEN", "message": "...", "details": {...}, "request_id": "..."}}
```

- Domain declines are 4xx with a specific code. A decline is never a 500 and never an unhandled exception.
- A catch-all handler converts anything unhandled into `INTERNAL_ERROR` 500, logs it with a stack, and increments an error counter. It must never leak an exception message to the client.
- Raise `AppError` subclasses from services; raise `HTTPException` nowhere but trivial framework-level checks.
- Database driver exceptions are translated at the repository boundary into domain errors. A `UniqueViolation` reaching a route is a layering bug.

## Middleware order

Declared so the effective inbound order is: request context → access log → metrics → rate limit → audit → application. Request id is established first so every later layer, including rate-limit rejections, is traceable. Audit enqueues without blocking.

## Auth

Every route declares its identity requirement through `Depends`, never by inspecting the request inside the handler:

- `Depends(get_current_user)` — any authenticated principal, including guests.
- `Depends(require_admin)` — admin role only.
- Public routes (`/healthz`, `/readyz`, auth endpoints) state that explicitly in a comment-free way: they simply do not declare a principal dependency, and the route table in `mds/06-apis.md` records the audience.

Identity comes only from the verified token. A `user_id` in a request body is ignored — not validated, ignored — and ownership checks compare against the token's subject. A route that reads an identity field from a payload is a security defect.

## Async discipline

The service is async end to end. No blocking call in a coroutine: no `time.sleep`, no sync driver, no sync file IO on the hot path. CPU-bound work (password hashing) runs in a thread pool with a bounded executor. Every outbound operation has a timeout.

## Comments

Code is self-documenting through naming and structure. Comments exist only where intent cannot be expressed in code: a non-obvious Postgres semantic the correctness argument depends on, a deliberate ordering, a cited invariant. No restating what the line does, no section banners, no TODO without an owner and a ledger ID.

## Enhancement log

- `2026-10-03` — Initial conventions: layering, no-helpers-in-routes, config/constants policy, request-id propagation, error envelope, middleware order, Depends-based auth, async discipline.
