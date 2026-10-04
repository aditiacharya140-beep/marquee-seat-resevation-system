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
| Reserve semantics | Returns `confirmed` by default; `hold_ttl_seconds` opts into a `held` reservation that `confirm` promotes (ADR-017) |
| Multi-seat partials | All-or-nothing; a single unavailable seat declines the whole request |
| Expiry | Lazy in the claim predicate only — **no sweeper**. Superseded claim rows are closed inside the claim transaction (ADR-019) |
| Per-user limit | Quota row lock to serialize a principal, count derived from `seats` |
| Idempotency | `(user_id, key)` unique; the fingerprint carries operation and show id; only successes are stored, a decline releases the key; every replay answers 200 (ADR-020/021/029) |
| Framework failures | Unmatched routes and bad methods answer inside the error envelope, not Starlette's default shape (ADR-023) |
| Guests | Real user rows flagged `is_guest`, token-derived like any principal, upgradeable |
| Rate limiting | Per-principal wherever a token exists, per client address only for sign-in and guest creation; every ceiling env-tunable (ADR-034) |
| Audit | One row per request through a bounded buffer that drops rather than waits, drained by a batched writer on its own connection (ADR-038) |
| Cancel | The owner can cancel a confirmed booking or a live hold; the release is guarded on the reservation's own id (ADR-036) |
| Key ownership | The idempotency key row's id is the ownership token: a stale takeover rotates it and the claim locks it first (ADR-033) |

## Document map

| Document | Contents |
|---|---|
| [01-requirements.md](01-requirements.md) | Numbered `REQ-*` requirements, acceptance criteria, traceability matrix, open questions |
| [02-architecture.md](02-architecture.md) | Layering, module layout, request lifecycle, transaction boundaries, generic event model |
| [03-data-model.md](03-data-model.md) | Tables, columns, constraints, indexes, the effective-status expression, migration policy |
| [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md) | The atomic claim, lock ordering and deadlock argument, per-user limit, idempotency lifecycle, holds and expiry, reconciliation |
| [05-auth-and-rbac.md](05-auth-and-rbac.md) | Token design, guest and upgrade flow, password policy, roles and permission matrix, `Depends` wiring, threat notes |
| [06-apis.md](06-apis.md) | Every endpoint: method, path, audience, request, response, status and error codes, idempotency and rate-limit behaviour |
| [07-middleware.md](07-middleware.md) | Chain order and rationale, request-id propagation, the access log, the rate limiter |
| [08-error-logging.md](08-error-logging.md) | `AppError` hierarchy, the error-code registry, response envelope, structured log schema, what is logged at which level |
| [09-repositories.md](09-repositories.md) | Repository contracts per aggregate, SQL ownership, driver-error translation, pooling, `request_id` stamping |
| [10-observability.md](10-observability.md) | Health and readiness, the metrics exposed, the audit trail, the admin console and its log view, alerting |
| [11-scalability.md](11-scalability.md) | Contention model, pool sizing, horizontal scaling and what breaks first, partition behaviour, known ceilings |
| [12-testing-and-burst.md](12-testing-and-burst.md) | Test strategy by layer, concurrency test design, burst script contract and output format |
| [13-deployment.md](13-deployment.md) | Container build, Render topology, configuration and secrets, cold start handling, rollback, operational runbook |
| [14-stage-plan.md](14-stage-plan.md) | Build stages, what is built, tested and reviewed in each, and what to do next |
| [15-tickets.md](15-tickets.md) | The original ticket board, with a status table at the top. Its `Done when` lines predate several ADRs and are not the authority where they disagree |
| [16-decision-highlights.md](16-decision-highlights.md) | Curated record of the judgment calls and the defects caught before shipping, with provenance |
| [17-future-scope.md](17-future-scope.md) | Everything designed or found to be needed that is **not** in the code |
| [99-ledger.md](99-ledger.md) | Append-only `ADR` / `LEARN` / `RISK` log |

## What this set describes

Documents 02 to 13 describe the service **as it is built and deployed**. Anything designed and not built — part of the metric catalogue, sale windows, a payment step — lives in [17-future-scope.md](17-future-scope.md) and nowhere else, so a statement in the other documents is a statement about the code (ADR-035).

## Invariants

Five statements that must hold at all times. Any code that can violate one is wrong regardless of test results.

1. A seat active for one principal — held and unexpired, or confirmed — can never become active for another.
2. `available + held + confirmed == total_seats`, sampled at any instant.
3. A domain outcome is 4xx with a specific code. Losing a race is never a 5xx.
4. One idempotency key produces one effect; a retry replays it; the same key with a different body is refused.
5. The acting principal is the token's subject. No request body can change who acts.

## Reading order

New to the project: this document, then [02-architecture.md](02-architecture.md), then [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md) — the last one is where the service earns or loses its claim to correctness.

Picking up work: [14-stage-plan.md](14-stage-plan.md) and [17-future-scope.md](17-future-scope.md) for what to build next, and [16-decision-highlights.md](16-decision-highlights.md) for why the design looks the way it does rather than the way it first did.
