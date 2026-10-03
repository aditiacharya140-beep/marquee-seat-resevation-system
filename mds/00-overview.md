# Overview

A JSON HTTP API that sells assigned seats for ticketed events. It is the system of record for who holds and who owns each seat, and its defining requirement is correctness under contention: tens of thousands of concurrent buyers, many fighting over the same seat, with exactly one winner per seat and no server errors anywhere in the loss path.

The domain is **generic seated events**. A cinema screening and a concert are the same model with different configuration — event kind, seat layout, pricing, per-user limit, hold duration. No vertical is named in a table, a column, or a module.

## Stack

| Concern | Choice |
|---|---|
| Runtime | Python 3.13, FastAPI, Uvicorn, async end to end |
| Datastore | PostgreSQL 16, single primary, `asyncpg` |
| Migrations | Alembic |
| Auth | JWT bearer, access + refresh, Argon2 password hashing |
| Metrics | `prometheus_client`, `/metrics` |
| Container | Dockerfile, multi-stage, non-root |
| Deploy | Render web service + Render managed Postgres, `render.yaml` |

Postgres is used in development and production identically. The atomic claim depends on Postgres row-locking semantics, so there is no second engine to diverge from.

## Core decisions

These are settled. Each is argued in its own document and recorded as an ADR in [99-ledger.md](99-ledger.md).

| Decision | Choice |
|---|---|
| Atomic claim | Guarded conditional `UPDATE`; ordered `FOR UPDATE` CTE for multi-seat |
| Reserve semantics | Returns `held` with a TTL; explicit `confirm` promotes it |
| Multi-seat partials | All-or-nothing; a single unavailable seat declines the whole request |
| Expiry | Lazy in the claim predicate, plus a background sweeper |
| Per-user limit | Quota row lock to serialize a principal, count derived from `seats` |
| Idempotency | `(user_id, key)` unique constraint; bounded wait then replay on overlap |
| Guests | Real user rows flagged `is_guest`, token-derived like any principal, upgradeable |
| Rate limiting | Per-principal, generous on the reserve path, every ceiling env-tunable |
| Audit | Bounded async queue, batched writer, never blocks a request |

## Document map

| Document | Contents |
|---|---|
| [01-requirements.md](01-requirements.md) | Numbered `REQ-*` requirements, acceptance criteria, traceability matrix, open questions |
| [02-architecture.md](02-architecture.md) | Layering, module layout, request lifecycle, transaction boundaries, generic event model |
| [03-data-model.md](03-data-model.md) | Tables, columns, constraints, indexes, the effective-status expression, migration policy |
| [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md) | The atomic claim, lock ordering and deadlock argument, per-user limit, idempotency lifecycle, holds and expiry, reconciliation |
| [05-auth-and-rbac.md](05-auth-and-rbac.md) | Token design, guest and upgrade flow, password policy, roles and permission matrix, `Depends` wiring, threat notes |
| [06-apis.md](06-apis.md) | Every endpoint: method, path, audience, request, response, status and error codes, idempotency and rate-limit behaviour |
| [07-middleware.md](07-middleware.md) | Chain order and rationale, request-id propagation, rate limiter design, audit enqueue, metrics capture |
| [08-error-logging.md](08-error-logging.md) | `AppError` hierarchy, the error-code registry, response envelope, structured log schema, what is logged at which level |
| [09-repositories.md](09-repositories.md) | Repository contracts per aggregate, SQL ownership, driver-error translation, pooling, `request_id` stamping |
| [10-observability.md](10-observability.md) | Health and readiness semantics, metric catalogue, audit table and async write path, trace correlation, alerting |
| [11-scalability.md](11-scalability.md) | Contention model, pool sizing, horizontal scaling and what breaks first, partition behaviour, known ceilings |
| [12-testing-and-burst.md](12-testing-and-burst.md) | Test strategy by layer, concurrency test design, burst script contract and output format |
| [13-deployment.md](13-deployment.md) | Container build, Render topology, configuration and secrets, cold start handling, rollback, operational runbook |
| [14-stage-plan.md](14-stage-plan.md) | Build stages with entry and exit gates, current status |
| [99-ledger.md](99-ledger.md) | Append-only `ADR` / `LEARN` / `RISK` log |

## Invariants

Five statements that must hold at all times. Any code that can violate one is wrong regardless of test results.

1. A seat active for one principal — held and unexpired, or confirmed — can never become active for another.
2. `available + held + confirmed == total_seats`, sampled at any instant.
3. A domain outcome is 4xx with a specific code. Losing a race is never a 5xx.
4. One idempotency key produces one effect; a retry replays it; the same key with a different body is refused.
5. The acting principal is the token's subject. No request body can change who acts.

## Reading order

New to the project: this document, then [02-architecture.md](02-architecture.md), then [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md) — the last one is where the service earns or loses its claim to correctness.
