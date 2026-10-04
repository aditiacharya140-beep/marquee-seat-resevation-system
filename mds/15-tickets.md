# Tickets

**67 tickets, `SEAT-001` … `SEAT-067`**, grouped under the stages in [14-stage-plan.md](14-stage-plan.md). The set is closed: no ticket is added without a corresponding entry in [01-requirements.md](01-requirements.md) or an explicit `ADR-*`, and no `REQ-*` is left without a ticket.

## How to read a ticket

| Field | Meaning |
|---|---|
| `Stage` | The stage whose gate this ticket contributes to. A ticket never spans stages. |
| `Covers` | The `REQ-*` ids this ticket advances. `enabling` means no requirement names it directly, with the justification stated. |
| `Depends on` | Tickets that must be closed first. Dependencies always point at lower numbers. |
| `Size` | `S` under half a sitting, `M` one sitting, `L` a full sitting with a single reviewable outcome. |
| `Scope` | The files and modules touched, from the layout in [02-architecture.md](02-architecture.md). |
| `Done when` | Observable checks. A status code, a row state, a constraint firing, a metric moving, a named test passing. |
| `Test` | The test file or case that proves it, per [12-testing-and-burst.md](12-testing-and-burst.md). |

Three rules govern the board:

1. **A ticket closes only when every `Done when` check is observably true.** "The code is written" closes nothing. A check that cannot be observed is a badly written check and gets rewritten, not waived.
2. **Dependency order is the build order.** A ticket never depends on a higher-numbered ticket. If one needs to, the decomposition is wrong and gets redone.
3. **A ticket that reveals its stage's design is wrong reopens the stage in the ledger.** It does not patch around the design. See the gate in [14-stage-plan.md](14-stage-plan.md).

| Stage | Tickets | Range |
|---|---|---|
| 0 Foundation | 7 | `SEAT-001` – `SEAT-007` |
| 1 Database and first deploy | 5 | `SEAT-008` – `SEAT-012` |
| 2 Auth and RBAC | 6 | `SEAT-013` – `SEAT-018` |
| 3 Shows and seats | 5 | `SEAT-019` – `SEAT-023` |
| 4 The atomic claim | 21 | `SEAT-024` – `SEAT-044` |
| 5 Lifecycle | 5 | `SEAT-045` – `SEAT-049` |
| 6 Observability | 5 | `SEAT-050` – `SEAT-054` |
| 7 Burst and hardening | 6 | `SEAT-055` – `SEAT-060` |
| 8 Documentation | 3 | `SEAT-061` – `SEAT-063` |
| 9 Monitoring view (optional) | 2 | `SEAT-064` – `SEAT-065` |

Stage 4 carries a third of the board deliberately. It is the stage the service is judged by, and it is the one place where a ticket that looks like one concern — "implement reserve" — hides four independent correctness mechanisms.

---

## Stage 0 — Foundation

Nothing here is domain logic, and everything later depends on all of it. The two tickets most likely to be underestimated are `SEAT-001` (neither Postgres nor Docker is currently installed) and `SEAT-005` (the clean-checkout container build, which is the single most common way a working service fails review).

### SEAT-001 — Provision the development environment and scaffold the repository
Stage:        0
Covers:       enabling — the environment prerequisites recorded at the end of `14-stage-plan.md`; Stage 1 cannot close without a local Postgres, and `REQ-050` cannot be verified locally without Docker.
Depends on:   none
Size:         M
Scope:        `pyproject.toml`, `Makefile`, `.gitignore`, `app/` package tree per the layout in `02-architecture.md` (empty `__init__.py` files), `tests/` and `burst/` directories, lint and type-check configuration
Done when:
  - `python --version` reports 3.13 or newer inside the project virtualenv; the system 3.9 is not on the path used by `make`.
  - `psql -c 'select version()'` reports PostgreSQL 16 against a local instance, reachable with the `DATABASE_URL` form the service will use.
  - `docker --version` succeeds and `docker run --rm hello-world` completes.
  - `make lint` and `make types` run and pass against the empty package tree.
  - Every directory in the `02-architecture.md` module layout exists; a diff of the layout against `find app -type d` is empty.
Test:         `make lint && make types` in CI (`SEAT-005` wires this into the pipeline)

### SEAT-002 — Typed settings with startup validation
Stage:        0
Covers:       REQ-050 (partial) — a fresh clone must refuse to boot on a missing secret rather than start with a default
Depends on:   SEAT-001
Size:         M
Scope:        `app/core/config.py`, `app/core/constants.py`, `.env.example`
Done when:
  - Every variable in the configuration table of `13-deployment.md` is declared as a typed field; no literal ceiling, TTL, batch size, or limit exists anywhere else in `app/`.
  - Omitting `JWT_SECRET` or `DATABASE_URL` raises at import of the settings object with a message naming the missing variable; the process exits non-zero.
  - `SeatStatus`, `ReservationStatus`, `ShowStatus`, `IdempotencyState`, `Role` and the header names are enums or constants in `constants.py`, with values matching the `CHECK` constraints in `03-data-model.md`. **No `EventKind` enum** — the permitted set is `ALLOWED_EVENT_KINDS` in configuration, and a grep for a vertical name in `app/` finds nothing (ADR-025).
  - A unit test asserts the resolved settings object round-trips every documented variable from the environment.
Test:         `tests/unit/test_config.py::test_missing_required_secret_fails_startup`, `::test_every_documented_variable_is_declared`

### SEAT-003 — JSON logging, the error hierarchy and the error envelope
Stage:        0
Covers:       REQ-045, REQ-048
Depends on:   SEAT-002
Size:         L
Scope:        `app/core/logging.py`, `app/core/errors.py`, `app/core/error_codes.py`
Done when:
  - Every emitted line is single-line JSON carrying `ts`, `level`, `event`, `request_id`, `service`, `version`; `event` values are snake_case identifiers from the catalogue in `08-error-logging.md`.
  - The redaction filter removes `password`, `password_hash`, `token`, `authorization`, and `DATABASE_URL` from any `extra=` payload; a test passing each of them asserts the value is absent from the output.
  - `AppError` and the subclass set in `08-error-logging.md` exist, each carrying `code`, `http_status`, `message`, `details`, `log_level`.
  - `error_codes.py` is the only place a code appears; a test asserts every code maps to exactly one status and no code is declared twice; the `06-apis.md` contract codes are a **subset** of the registry, and the surplus is exactly the operational codes mandated by `08-error-logging.md` (`DATABASE_UNAVAILABLE`, `NOT_READY`, `INTERNAL_ERROR`) plus the framework-failure codes from ADR-023. Equality is not assertable — those codes appear in no contract table.
Test:         `tests/unit/test_error_registry.py`, `tests/unit/test_logging_redaction.py`

### SEAT-004 — App factory, request correlation, access log and `/healthz`
Stage:        0
Covers:       REQ-040, REQ-044, REQ-045
Depends on:   SEAT-003
Size:         L
Scope:        `app/main.py`, `app/core/context.py`, `app/middleware/request_context.py`, `app/middleware/access_log.py`, `app/api/routes/health.py`, `app/api/routes/__init__.py`
Done when:
  - Middleware registration produces the inbound order in `07-middleware.md`; a test asserts the effective order by observing which layer sees a failure raised before route resolution.
  - A valid UUID in `X-Request-ID` is adopted; a malformed value is replaced by a minted UUID; both are echoed in the response `X-Request-ID` header and appear in that request's log lines.
  - The context var is reset in a `finally`; a test issuing two sequential requests on one task asserts the second never sees the first's id.
  - Exception handlers for `AppError`, `RequestValidationError`, and a catch-all are registered and render the envelope from `08-error-logging.md`, 422 field errors under `details.fields`, with no `str(exc)` in any body.
  - `GET /healthz` returns 200 with `{status, service, version}`, opens no connection, and is excluded from the access log.
Test:         `tests/integration/test_ops.py::test_healthz_touches_no_dependency`, `tests/integration/test_request_context.py`

### SEAT-005 — Container build, local compose and CI
Stage:        0
Covers:       REQ-050
Depends on:   SEAT-004
Size:         L
Scope:        `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `entrypoint.sh`, `.github/workflows/ci.yml`
Done when:
  - A build from a clean checkout of the current commit produces an image that runs as a non-root user with uvicorn as PID 1 and the port taken from `$PORT`.
  - `docker compose up` brings up Postgres 16 and the service with no manual step; `GET /healthz` on the mapped port returns 200.
  - `.dockerignore` excludes `tests/`, `burst/`, `.git`, and local env files; `docker run --rm <image> ls` shows none of them present.
  - CI runs lint, types, and unit tests, then builds the image and asserts `/healthz` 200 from the built image, all from a fresh clone with no cached state.
  - The dependency set is lockfile-pinned; two consecutive builds resolve identical versions.
Test:         CI job `container-smoke` (the `REQ-050` check named in `12-testing-and-burst.md`)

### SEAT-006 — Foundation integration tests
Stage:        0
Covers:       REQ-040, REQ-044, REQ-045
Depends on:   SEAT-004
Size:         M
Scope:        `tests/conftest.py`, `tests/integration/test_request_context.py`, `tests/integration/test_ops.py`
Done when:
  - An ASGI test client fixture exists that does not require a database, so Stage 0 tests run before Stage 1 lands.
  - Correlation is asserted on a success, a 422, and a deliberately raised unhandled exception: all three carry the same id in the header, the envelope, and the log line.
  - A test asserts the catch-all handler returns 500 `INTERNAL_ERROR` with a generic message and that the log line for it carries a stack trace.
  - `/healthz` latency is asserted under the configured single-digit-millisecond budget.
Test:         `tests/integration/test_request_context.py`, `tests/integration/test_ops.py`

### SEAT-007 — grill: Stage 0
Stage:        0
Covers:       enabling — the stage gate in `14-stage-plan.md` requires a `grill` pass before Stage 1 opens
Depends on:   SEAT-005, SEAT-006
Size:         S
Scope:        review only; findings land as fixes on `SEAT-002` … `SEAT-006` or as `RISK-*` entries in `mds/99-ledger.md`
Done when:
  - Attack list covered in writing: a secret present in the image or a log line, a hardcoded tunable, a code defined in two places, a context var that leaks across requests, a handler that leaks internals, an image that builds only with a warm cache.
  - Every finding is fixed, refuted in writing, or recorded as `RISK-*` with a trigger and a mitigation.
  - The ledger carries the Stage 0 review entry.
Test:         n/a — the review output is the artifact; regressions are caught by the tests named above

---

## Stage 1 — Database and first deploy

The point of this stage is to discover platform problems while the service is still trivial. `SEAT-010` is the ticket that earns the stage: a deploy that comes up proves the pipeline, and from here the live URL is never left broken.

### SEAT-008 — Connection pool, session guards and the `users` migration
Stage:        1
Covers:       enabling — pool sizing is the arithmetic `REQ-048` rests on (`11-scalability.md`), and every later migration needs the Alembic scaffolding
Depends on:   SEAT-002, SEAT-005
Size:         L
Scope:        `app/db/engine.py`, `app/db/session.py`, `app/db/migrations/` (Alembic env and first revision), `app/repositories/base.py`
Done when:
  - The `asyncpg` pool is created in the lifespan handler and closed on shutdown; nothing opens a connection at import time.
  - Every checked-out connection has `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout` and `TimeZone=UTC` applied from config; a test reads `SHOW` for each and asserts the configured value.
  - Pool max, min and acquire timeout come from config; a pool max exceeding the configured database ceiling refuses to boot.
  - `alembic upgrade head` on an empty database creates `users` with `uq_users_email` and `ck_users_creds`; `alembic downgrade base` then `upgrade head` succeeds.
  - Inserting a half-upgraded guest (`is_guest=true` with an email) raises a check violation, asserted directly.
  - `repositories/base.py` stamps `request_id` from the context var on insert and update without receiving it as a parameter.
Test:         `tests/integration/test_migrations.py`, `tests/integration/test_session_guards.py`

### SEAT-009 — `/readyz` with a real query, failing closed
Stage:        1
Covers:       REQ-041
Depends on:   SEAT-008
Size:         M
Scope:        `app/api/routes/health.py`, `app/db/session.py`
Done when:
  - `GET /readyz` executes `SELECT 1` on a pooled connection with a short timeout and returns 200 with `checks.database.ok` and `latency_ms`.
  - With the database stopped it returns 503 with `checks.database.ok=false` and the driver's reason in `checks.database.error`, and logs `readiness_check_failed`.
  - No caching: two calls with the database stopped between them produce 200 then 503 with no interval of staleness.
  - `/healthz` stays 200 throughout, asserted in the same test.
  - The response reports whether rate limiting is enabled (field present from this ticket, value wired in `SEAT-052`).
Test:         `tests/integration/test_ops.py::test_readyz_fails_closed`, `::test_readyz_is_never_cached`

### SEAT-010 — `render.yaml` and the first deploy
Stage:        1
Covers:       REQ-049, REQ-050
Depends on:   SEAT-009
Size:         M
Scope:        `render.yaml`, `entrypoint.sh`, deployment configuration
Done when:
  - `render.yaml` declares the web service (Docker runtime, health check path `/readyz`) and the managed Postgres 16 instance; no setting is applied by console click only.
  - The entrypoint runs `alembic upgrade head` then `exec`s uvicorn; the deploy log shows migrations applied against a fresh database.
  - The live URL returns 200 on `/healthz` and `/readyz`; the startup log line shows the resolved configuration with secrets redacted.
  - A cold start after idle spin-down reaches `/readyz` ready within the configured budget, measured and recorded.
Test:         post-deploy verification steps 1–5 of `13-deployment.md`, re-run by `SEAT-011`

### SEAT-011 — Operational tests: readiness, liveness and cold start
Stage:        1
Covers:       REQ-040, REQ-041, REQ-049
Depends on:   SEAT-010
Size:         M
Scope:        `tests/integration/test_ops.py`, `tests/conftest.py` (database fixture: per-session create and migrate)
Done when:
  - A session-scoped fixture creates and migrates a real Postgres database; no test mocks the database, anywhere.
  - Readiness-fails-closed and liveness-unaffected are a single test that stops and restarts the dependency.
  - A cold-start test launches the container from scratch and asserts `/readyz` becomes ready inside the configured budget.
  - The suite passes in CI against a Postgres service container.
Test:         `tests/integration/test_ops.py`

### SEAT-012 — grill: Stage 1
Stage:        1
Covers:       enabling — stage gate
Depends on:   SEAT-011
Size:         S
Scope:        review only
Done when:
  - Attack list covered in writing: readiness that could answer from a cache, a pool sized by request concurrency rather than the database ceiling, an acquire timeout shorter than the expected queue drain, a migration that is not rollback-safe, a session guard missing on a connection acquired by a worker rather than a request.
  - Every finding fixed, refuted in writing, or recorded as `RISK-*`.
  - Live URL verified healthy at the close of the review.
Test:         n/a

---

## Stage 2 — Auth and RBAC

Identity-is-token-derived is a correctness property, not a prerequisite to rush. `SEAT-017` is the ticket that proves it, and it is deliberately the largest in the stage.

### SEAT-013 — `core/security.py`: Argon2 hashing and the JWT codec
Stage:        2
Covers:       REQ-002, REQ-006
Depends on:   SEAT-002, SEAT-003
Size:         L
Scope:        `app/core/security.py`, `app/utils/hashing.py`
Done when:
  - Argon2id hashing and verification run in a bounded thread pool whose size comes from config; a test firing more concurrent hashes than the pool width asserts the event loop still serves `/healthz` within its budget.
  - Password policy (minimum length, maximum not below 128, no composition rules) is read from config and rejects below-policy input with a field-level error.
  - A verification miss path hashes a dummy value, so a hit and a miss have comparable latency; a test asserts the two medians are within the configured tolerance.
  - The JWT codec emits the claim set in `05-auth-and-rbac.md` and rejects, each with 401 and no detail about which check failed: missing, malformed, bad signature, wrong `iss`, wrong `typ`, expired, `alg: none`, and an asymmetric algorithm substitution.
  - No token, secret, or hash is passed to any log call; asserted by the redaction test from `SEAT-003` extended with a token payload.
Test:         `tests/unit/test_security.py`, `tests/unit/test_jwt_codec.py`

### SEAT-014 — `user_repo.py` and the auth dependency providers
Stage:        2
Covers:       REQ-005, REQ-006, REQ-007
Depends on:   SEAT-008, SEAT-013
Size:         M
Scope:        `app/repositories/user_repo.py`, `app/api/deps.py`
Done when:
  - `user_repo` exposes exactly the methods in `09-repositories.md`; no method opens, commits, or rolls back a transaction.
  - `get_by_email` lowercases to match `uq_users_email`; a duplicate insert surfaces as 409 `EMAIL_TAKEN` translated at the repository boundary, never from a prior existence check.
  - `get_current_user` verifies the token, builds a `Principal`, and binds it to the context var; `require_user` asserts `typ=access`; `require_admin` returns 403 `FORBIDDEN` for a non-admin.
  - No authenticated request schema declares an identity field; a test introspects every authenticated Pydantic model and asserts no `user_id`-like field exists.
  - A route with no principal dependency cannot read the principal: the context var is the only source, and it is unset for anonymous requests.
Test:         `tests/integration/test_authz.py::test_no_request_model_declares_identity`, `tests/unit/test_deps.py`

### SEAT-015 — Register, login, refresh, `/auth/me` and the admin bootstrap
Stage:        2
Covers:       REQ-001, REQ-002, REQ-008
Depends on:   SEAT-014
Size:         L
Scope:        `app/api/routes/auth.py`, `app/services/auth_service.py`, `app/schemas/auth.py`, `app/main.py` (bootstrap in lifespan)
Done when:
  - `POST /auth/register` returns 201 with the body in `06-apis.md`, a row with role `user`, `is_guest=false`, and an Argon2 hash; a duplicate email returns 409 `EMAIL_TAKEN` and creates no second row, asserted by counting rows.
  - `POST /auth/login` returns 200 with access and refresh tokens; invalid credentials return 401 `INVALID_CREDENTIALS` with an identical message for an unknown email and a wrong password.
  - `POST /auth/refresh` accepts only `typ=refresh` and returns a new access token; a refresh token presented as a bearer credential on a business route returns 401.
  - `GET /auth/me` returns the token subject's own record and nothing else.
  - Admin bootstrap runs in the lifespan, creates an admin only when none exists, logs the fact loudly, and is a no-op on a second start; a test starting the app twice asserts exactly one admin row and an unchanged password hash.
Test:         `tests/integration/test_auth.py`

### SEAT-016 — Guest identity, guest upgrade and the guest-TTL startup check
Stage:        2
Covers:       REQ-003, REQ-004
Depends on:   SEAT-015
Size:         L
Scope:        `app/api/routes/auth.py`, `app/services/auth_service.py`, `app/repositories/user_repo.py`, `app/core/config.py`
Done when:
  - `POST /auth/guest` returns 201 with an access token and no refresh token, and creates a row with `is_guest=true`, `email NULL`, `password_hash NULL`.
  - `POST /auth/upgrade` with a guest token and an unused email returns 200 with the **same** `user_id` and `is_guest=false`, applied by the single guarded `UPDATE` from `05-auth-and-rbac.md`.
  - A non-guest token returns 409 `ALREADY_REGISTERED`; a taken email returns 409 `EMAIL_TAKEN` and the row remains a guest, asserted by re-reading it.
  - Two concurrent upgrades of one guest produce exactly one 200 and one 409; `ck_users_creds` is never violated.
  - Startup refuses to boot when `GUEST_TOKEN_TTL_SECONDS` does not exceed `DEFAULT_HOLD_TTL_SECONDS` plus the configured margin, with a message naming both values.
Test:         `tests/integration/test_auth.py::test_guest_upgrade_preserves_user_id`, `tests/unit/test_config.py::test_guest_ttl_must_exceed_hold_ttl`

### SEAT-017 — Integration: auth flows and authorization probes
Stage:        2
Covers:       REQ-001, REQ-002, REQ-003, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008
Depends on:   SEAT-016
Size:         L
Scope:        `tests/integration/test_auth.py`, `tests/integration/test_authz.py`
Done when:
  - Every bullet in the authorization-probe list of `12-testing-and-burst.md` is a distinct named test: spoofed identity in the body, cross-principal access, a `user` and a `guest` token on every admin route, refresh-as-access, expired token, tampered signature, wrong issuer, `alg: none`.
  - The admin-route probe is parameterized over the admin routes in the `06-apis.md` route table, so a route added later without a probe fails the parameterization count assertion.
  - A guest performs the full reserve-confirm-cancel path once Stage 4 and 5 land; the placeholder in this ticket asserts the guest token is accepted by `require_user` and refused by `require_admin`.
  - No state change accompanies any 401 or 403, asserted by row counts before and after.
Test:         `tests/integration/test_auth.py`, `tests/integration/test_authz.py`

### SEAT-018 — grill: Stage 2
Stage:        2
Covers:       enabling — stage gate
Depends on:   SEAT-017
Size:         S
Scope:        review only
Done when:
  - Attack list covered in writing: a fetch-then-compare ownership check, a role read from a body, an identity field reachable through an unvalidated extra field, a token accepted with the wrong `typ`, a timing oracle on email existence, an unbounded hashing pool, a bootstrap that can overwrite a changed password.
  - Every finding fixed, refuted in writing, or recorded as `RISK-*`; `RISK-002` (role staleness within an access-token lifetime) confirmed as still accepted.
Test:         n/a

---

## Stage 3 — Shows and seats

The schema and the effective-status expression land here. `SEAT-019` is load-bearing far beyond this stage: every correctness property in Stage 4 rests on `uq_seats_show_label` and `ck_seats_hold_coherent` existing exactly as written.

### SEAT-019 — Migrations for `shows`, `seats`, and the effective-status expression
Stage:        3
Covers:       REQ-012, REQ-013
Depends on:   SEAT-008
Size:         M
Scope:        `app/db/migrations/` (one revision), `app/db/sql.py`
Done when:
  - `shows` and `seats` exist with every column, `CHECK`, unique constraint and index quoted in `03-data-model.md`, including `uq_seats_show_label`, `ck_seats_hold_coherent`, `ix_seats_claimable`, `ix_seats_expiring`, `ix_seats_by_holder`.
  - A direct attempt to write `status='held'` with `held_by IS NULL`, and `status='available'` with a non-null `hold_expires_at`, each raise `ck_seats_hold_coherent`; both asserted.
  - Two seats with the same `(show_id, label)` raise `uq_seats_show_label`.
  - `db/sql.py` defines the effective-status expression and the counts query **once**; a test greps `app/` and asserts the `CASE WHEN status = 'confirmed'` fragment appears in exactly one file.
  - `alembic downgrade base` then `upgrade head` succeeds against a populated database.
Test:         `tests/integration/test_migrations.py::test_seat_constraints_reject_incoherent_state`, `tests/unit/test_sql_fragments.py::test_effective_status_defined_once`

### SEAT-020 — Show creation: policies, helpers, repository and route
Stage:        3
Covers:       REQ-010, REQ-011, REQ-060
Depends on:   SEAT-014, SEAT-019
Size:         L
Scope:        `app/domain/policies.py`, `app/domain/models.py`, `app/helpers/seat_labels.py`, `app/helpers/pricing.py`, `app/repositories/show_repo.py`, `app/services/show_service.py`, `app/api/routes/shows.py`, `app/schemas/shows.py`
Done when:
  - `POST /shows` as an admin returns 201 with the body in `06-apis.md`, `total_seats` equal to the label count, one `available` seat row per label, and `counts` satisfying the invariant.
  - Show and all seats are created in one transaction with a single multi-row insert; a 2,000-seat show issues one insert statement, asserted by a statement counter.
  - Each of these returns 422 `VALIDATION_ERROR` naming the offending field and creates **zero** rows, asserted by counting `shows` and `seats`: empty `seats`, duplicate labels, negative `price_paise`, non-integer `price_paise`, a label over the configured length, a seat count over the configured maximum, an override naming a label not in `seats`.
  - `per_user_limit`, `hold_ttl_seconds`, `event_kind` and `currency` default from config when omitted; `seat_overrides` applies per-seat `price_paise` and layout metadata.
  - A non-admin receives 403 `FORBIDDEN` and creates nothing.
  - Price resolution is `COALESCE(seats.price_paise, shows.price_paise)` in one place, integer paise throughout.
Test:         `tests/integration/test_shows.py::test_create_show_is_atomic`, `::test_invalid_show_creates_nothing`

### SEAT-021 — Show reads: `GET /shows/{show_id}`
Stage:        3
Covers:       REQ-012, REQ-013
Depends on:   SEAT-020
Size:         L
Scope:        `app/repositories/show_repo.py`, `app/services/show_service.py`, `app/api/routes/shows.py`, `app/schemas/shows.py`, `app/helpers/pagination.py`
Done when:
  - `GET /shows/{show_id}` returns per-seat status and `counts` from one statement using the `db/sql.py` expression; `available + held + confirmed == total` on every response, asserted over a randomized seat-state fixture.
  - `held_by` appears in no response; `held_until` appears only on a `held` seat. A test asserts the serialized keys exactly match the contract.
  - An unknown id returns 404 `SHOW_NOT_FOUND`.
  - `GET /shows` is keyset-paginated on `(created_at, id)` with an opaque cursor, `limit` bounded by config, and does not scan seat rows — asserted by a statement counter that stays constant as seat counts grow.
  - The lapsed-but-unswept case is left to `SEAT-047`, which can create a hold; this ticket asserts the expression's behaviour directly against a crafted row in `tests/unit/test_sql_fragments.py`.
Test:         `tests/integration/test_shows.py::test_counts_hold_invariant`, `::test_list_shows_does_not_scan_seats`

### SEAT-022 — Unit and integration tests: labels, policies, money and the show suite
Stage:        3
Covers:       REQ-010, REQ-011, REQ-012, REQ-013, REQ-014, REQ-060
Depends on:   SEAT-021
Size:         L
Scope:        `tests/unit/test_seat_labels.py`, `tests/unit/test_policies.py`, `tests/unit/test_money.py`, `tests/integration/test_shows.py`
Done when:
  - Label validation, normalization, duplicate detection and length bounds are each a named unit test.
  - Policy tests cover limit arithmetic, TTL clamping, and price resolution with and without a per-seat override.
  - `tests/unit/test_money.py` asserts exact integer totals at large quantities (2,000 seats at an awkward price) and that no money value passes through a float.
  - The show suite covers `REQ-010` through `REQ-014` with a named test per requirement, each referencing its id in the test docstring.
Test:         the files named in Scope

### SEAT-023 — grill: Stage 3
Stage:        3
Covers:       enabling — stage gate
Depends on:   SEAT-022
Size:         S
Scope:        review only
Done when:
  - Attack list covered in writing: a second copy of the effective-status expression, counts read from two statements, a seat row insertable after creation, a fourth seat state introduced by a default, `total_seats` mutable, a loop of inserts for a large hall, a show read that exposes `held_by`.
  - Every finding fixed, refuted in writing, or recorded as `RISK-*`.
Test:         n/a

---

## Stage 4 — The atomic claim

**No other stage runs in parallel with this one.** Stage 3's gate is closed before `SEAT-024` opens and Stage 5's first ticket does not start until `SEAT-044` closes. This stage gets undivided attention and the most adversarial review, because it is the stage the service is judged by.

The decomposition is deliberate: the two claim statements, the quota lock, the idempotency lifecycle, and the reserve orchestration are separate tickets with separate tests. Each concurrency test in `12-testing-and-burst.md` is its own ticket, because those tests are the deliverable evidence rather than a check on the deliverable.

Two standing rules apply to every ticket in this stage, taken from `04-concurrency-and-atomicity.md`:

- **No `SELECT` then `UPDATE` in the claim path**, in any wrapper. **No `SKIP LOCKED` anywhere** — ADR-017 removed the sweeper, which was its only legitimate use.
- **No transaction acquires a quota lock after acquiring a seat lock.** Any ticket that adds a path doing so is rejected at review, not fixed later.

### SEAT-024 — Migrations: `reservations`, `reservation_seats`, `user_show_quota`, `idempotency_keys`
Stage:        4
Covers:       REQ-021 (the `uq_seat_active_claim` backstop), REQ-023 (the quota lock target), REQ-024 (key storage)
Depends on:   SEAT-019
Size:         L
Scope:        `app/db/migrations/` (one revision), `app/domain/models.py`
Done when:
  - All four tables exist with every column, `CHECK`, FK and index quoted in `03-data-model.md`.
  - `uq_seat_active_claim` exists as a partial unique index on `reservation_seats (seat_id) WHERE released_at IS NULL`; inserting a second active claim row for one `seat_id` raises a unique violation, asserted directly.
  - `uq_idem_user_key` exists on `(user_id, key)`; the same key for two different users inserts cleanly, the same key twice for one user raises.
  - `ix_idem_stale`, `ix_idem_expiry`, `ix_reservations_user_show`, `ix_reservations_expiring` exist and are used by their intended queries, asserted by `EXPLAIN` in the migration test.
  - `user_show_quota` has the composite primary key and stores no count column — a test asserts the column set is exactly `(user_id, show_id, created_at)`.
  - `alembic downgrade base` then `upgrade head` succeeds against a populated database.
Test:         `tests/integration/test_migrations.py::test_active_claim_backstop_rejects_second_claim`, `::test_quota_table_stores_no_tally`

### SEAT-025 — `seat_repo.claim_one`: the guarded conditional UPDATE
Stage:        4
Covers:       REQ-020, REQ-021
Depends on:   SEAT-024
Size:         M
Scope:        `app/repositories/seat_repo.py`, `app/db/sql.py`
Done when:
  - The statement is byte-for-byte the one in `04-concurrency-and-atomicity.md`, with the expiry arm sourced from `db/sql.py` and all values bound, not interpolated.
  - One row returned means ownership; zero rows returns `None` and the repository raises nothing — a decline is a return value, not an exception.
  - `price_paise` is returned as `COALESCE(seats.price_paise, $show_price)`, an integer.
  - `version` increments and `request_id` is stamped from the context var on every successful claim, asserted by re-reading the row.
  - No `SELECT` precedes the `UPDATE` in this method or any caller added by this ticket; asserted by a static check that the claim path contains exactly one statement.
Test:         `tests/integration/test_seat_repo.py::test_claim_one_returns_none_when_taken`, `::test_claim_one_stamps_version_and_request_id`

### SEAT-026 — `seat_repo.claim_many`: the ordered `FOR UPDATE` CTE
Stage:        4
Covers:       REQ-020, REQ-022
Depends on:   SEAT-025
Size:         M
Scope:        `app/repositories/seat_repo.py`
Done when:
  - The statement is the CTE in `04-concurrency-and-atomicity.md`, including `ORDER BY label` inside `FOR UPDATE`; a comment records that the sort is the deadlock prevention and must not be removed.
  - The method returns every row it claimed and makes no policy decision; the shortfall comparison belongs to the service.
  - Claiming three labels where one is already held returns two rows; the caller's rollback leaves **zero** of the three held, asserted by re-reading all three.
  - A label not in the show is simply absent from the result, leaving the unknown-label case to the preflight in `SEAT-034`.
  - `SKIP LOCKED` appears nowhere in this module; asserted by a static check over `app/repositories/seat_repo.py` excluding `sweep_expired`.
Test:         `tests/integration/test_seat_repo.py::test_claim_many_is_all_or_nothing_on_rollback`

### SEAT-027 — Claim-predicate regression probes
Stage:        4
Covers:       REQ-021, REQ-033 (the lazy-expiry arm), REQ-048
Depends on:   SEAT-026
Size:         M
Scope:        `tests/concurrency/test_claim_semantics.py`
Done when:
  - The four `LEARN-002` probes are automated rather than trusted as a one-off manual result: a loser blocks then re-evaluates and reports zero rows; 50 concurrent claimers on one seat produce 1 winner, 49 losers and **zero** errors; a winner that rolls back lets the blocked claimer win; a lapsed hold is claimable with no worker involved — which under ADR-017 is the entire expiry mechanism, not a fallback.
  - The zero-errors assertion is explicit — a loser must be a return value, never a raised driver exception.
  - The suite runs against the real PostgreSQL 16 instance and is wired into CI at reduced parallelism.
  - The file header names `LEARN-002` and states that this is the minimum regression set for any change to the claim predicate.
Test:         `tests/concurrency/test_claim_semantics.py`

### SEAT-028 — Quota lock and the derived per-user count
Stage:        4
Covers:       REQ-023
Depends on:   SEAT-024
Size:         M
Scope:        `app/repositories/seat_repo.py` (`count_active_for_user`), `app/repositories/base.py`, `app/domain/policies.py`
Done when:
  - The upsert-then-`FOR UPDATE` pair on `user_show_quota` is one repository method, issued as the two statements in `04-concurrency-and-atomicity.md`, and is the first lock any claiming transaction takes.
  - `count_active_for_user` counts from `seats` using the effective-status expression from `db/sql.py`; no tally column is read or written anywhere.
  - `EXPLAIN` shows the count served by `ix_seats_by_holder`, index-only.
  - A unit test over `domain/policies.py` asserts the decision `count + len(requested) > per_user_limit` at the boundary values: one below, exactly at, and one above.
  - Two different principals claiming concurrently never block on each other at the quota step, asserted by a test that would time out if they did.
Test:         `tests/integration/test_seat_repo.py::test_quota_lock_serializes_one_principal`, `tests/unit/test_policies.py::test_limit_boundary`

### SEAT-029 — `utils/canonical_json.py` and the request fingerprint
Stage:        4
Covers:       REQ-025
Depends on:   SEAT-002
Size:         M
Scope:        `app/utils/canonical_json.py`, `app/utils/hashing.py`
Done when:
  - Canonicalization sorts object keys, normalizes whitespace, sorts and de-duplicates seat labels, and omits null-valued optional fields.
  - `{"seats":["A12","A13"]}` and `{ "seats": ["A13", "A12"] }` produce one identical SHA-256 fingerprint; changing any seat, the show, or the TTL produces a different one.
  - The function is pure and imports nothing from the project — asserted by a static import check, per the `utils` row of the layering table in `02-architecture.md`.
  - Unit tests cover reordering, whitespace, casing, nesting, and numeric formatting (`120` versus `120.0` must not both be accepted as the same request body — a float is rejected upstream by the schema).
Test:         `tests/unit/test_canonical_json.py`

### SEAT-030 — `idempotency_repo`: claim, complete, release and stale reclaim
Stage:        4
Covers:       REQ-024, REQ-026
Depends on:   SEAT-024, SEAT-029
Size:         L
Scope:        `app/repositories/idempotency_repo.py`
Done when:
  - `try_claim` runs in **its own transaction** and commits before returning, so ownership is visible to a concurrent duplicate immediately; the signature takes no connection, which enforces that at the type level.
  - `complete` **takes a connection**, so it commits inside the reservation's transaction; the signature difference is documented in the module docstring with the argument from `04-concurrency-and-atomicity.md`.
  - A unique violation on `uq_idem_user_key` is not an error: `try_claim` returns the existing row as a `ClaimOutcome`, never raising past the repository boundary.
  - `release` deletes the row, used only on an unexpected fault so a client may genuinely re-attempt.
  - `reclaim_if_stale` takes the row `FOR UPDATE`, re-checks `created_at` against the configured staleness window, and resets ownership; a test asserts a row younger than the window is not reclaimed and one older is, logging `idempotency_key_reclaimed`.
  - `purge_expired` deletes by `expires_at` in bounded batches.
Test:         `tests/integration/test_idempotency_repo.py::test_claim_is_visible_before_reservation_commits`, `::test_stale_key_reclaim_respects_window`

### SEAT-031 — `idempotency_service`: the decision tree
Stage:        4
Covers:       REQ-024, REQ-025, REQ-026
Depends on:   SEAT-030
Size:         L
Scope:        `app/services/idempotency_service.py`, `app/core/metrics.py` (counter declarations consumed in Stage 6)
Done when:
  - Every branch of the flow diagram in `04-concurrency-and-atomicity.md` is implemented and has a named test: own-the-key, fingerprint mismatch, completed-replay, in-progress-poll-then-complete, row-disappeared-retry-once, wait-bound-exhausted.
  - Fingerprint mismatch returns 409 `IDEMPOTENCY_KEY_REUSED` **before any seat work**, asserted by a seat-state snapshot taken before and after.
  - A completed key replays the stored status code and body byte-for-byte with header `Idempotent-Replay: true`, including a stored 409 decline replayed as a 409.
  - The in-progress poll interval and maximum wait come from config; exhausting the bound returns 409 `IDEMPOTENCY_IN_PROGRESS` with `Retry-After` and creates no second reservation.
  - `scope` is `reserve:{show_id}`, so one key presented against a different show is a distinct operation rather than a wrong replay.
  - An unexpected fault releases the key; a 422 is rejected before the key is claimed at all.
Test:         `tests/concurrency/test_idempotency.py` (unit-level branches in `tests/unit/test_idempotency_decisions.py`)

### SEAT-032 — `reservation_repo.create`
Stage:        4
Covers:       REQ-020
Depends on:   SEAT-024
Size:         M
Scope:        `app/repositories/reservation_repo.py`
Done when:
  - One call inserts the `reservations` row and all `reservation_seats` rows in one multi-row statement, on the connection it is handed; it opens no transaction.
  - `reservation_seats` captures `label` and `price_paise` at claim time; a later change to `shows.price_paise` does not alter a recorded row, asserted by a test that changes the price and re-reads.
  - `seat_count` and `amount_paise` are integers and `amount_paise` equals the sum of the captured seat prices, asserted at 2,000 seats.
  - `get_owned(conn, reservation_id, user_id)` exists and there is **no** `get(reservation_id)`; asserted by a test that introspects the module's public names.
  - A second active claim row for one seat raises the backstop violation, which the repository translates to 409 `SEAT_TAKEN` and logs at `error`.
  - **Superseded rows are closed before the new ones are inserted** (ADR-019): `UPDATE reservation_seats SET released_at = now() WHERE seat_id = ANY($1) AND released_at IS NULL`, as a **separate statement** in the same transaction, never folded into the insert's CTE — sub-statements of one `WITH` share a snapshot and have no defined order, so the insert could be evaluated first and trip `uq_seat_active_claim` anyway (LEARN-009).
  - A claim against a lapsed, unswept hold **succeeds** rather than violating the backstop index, asserted directly: set a hold's `hold_expires_at` into the past, claim the seat as another principal, and expect 201 with exactly one active `reservation_seats` row. Without the closure above this test returns 409, so it fails against the un-fixed implementation.
Test:         `tests/integration/test_reservation_repo.py::test_price_is_captured_at_claim_time`, `::test_no_unfiltered_getter_exists`, `::test_claim_against_lapsed_hold_succeeds`

### SEAT-033 — `reservation_service.reserve`: the two-transaction orchestration
Stage:        4
Covers:       REQ-020, REQ-022, REQ-023, REQ-029
Depends on:   SEAT-025, SEAT-026, SEAT-028, SEAT-031, SEAT-032
Size:         L
Scope:        `app/services/reservation_service.py`
Done when:
  - T1 and T2 are exactly the split in `02-architecture.md`: the key claim commits alone; the quota lock, limit check, claim, reservation insert and key completion commit together.
  - Lock order is quota row, then seats in ascending label order, in every path through the method; a review note in the docstring states the invariant and a test asserts no path reaches `claim_*` before the quota lock.
  - A shortfall from `claim_many` raises `SeatTakenError` with `details.conflicts` equal to requested minus claimed, and the transaction rolls back so nothing is held.
  - A limit breach raises `PerUserLimitError` with `details.limit` and `details.currently_held`, before any seat is touched.
  - A show that is `draft`, `closed`, or outside its sale window raises 409 `SHOW_NOT_ON_SALE`, checked before the quota lock.
  - If T2 rolls back, the key remains `in_progress` and no reservation exists; a test forcing a rollback asserts both.
  - Single-seat requests use `claim_one` and multi-seat requests use `claim_many`; both paths produce an identical response shape.
Test:         `tests/integration/test_reserve.py`, `tests/concurrency/test_double_sell.py` (added by `SEAT-039`, `SEAT-040`)

### SEAT-034 — `POST /shows/{show_id}/reserve`: route, schemas, key extraction and label preflight
Stage:        4
Covers:       REQ-005, REQ-020, REQ-027, REQ-028
Depends on:   SEAT-033
Size:         L
Scope:        `app/api/routes/reservations.py`, `app/schemas/reservations.py`, `app/api/deps.py`, `app/repositories/seat_repo.py` (`labels_not_in_show`)
Done when:
  - The handler resolves dependencies, calls one service method, and returns a response model; it contains no branching on business state.
  - The request schema declares no identity field and forbids unknown fields; a body carrying `user_id` returns 422, and a body carrying `user_id` where the schema tolerates it is impossible by construction.
  - The idempotency key is taken from the `Idempotency-Key` header or the body; absent, over the configured length, or present in both places with different values each return 422 `VALIDATION_ERROR`.
  - Preflight, before the key is claimed: a label not in the show returns 404 `SEAT_NOT_FOUND` naming it; a label repeated in one request returns 422; more labels than `per_user_limit` returns 422.
  - `hold_ttl_seconds` is optional and clamped to the show's configured maximum.
  - The 201 body matches `06-apis.md` exactly, with `user_id` equal to the token subject and `seats` sorted.
Test:         `tests/integration/test_reserve.py::test_key_sources_and_conflicts`, `tests/integration/test_authz.py::test_spoofed_identity_is_ignored`

### SEAT-035 — Claim-path driver-error translation
Stage:        4
Covers:       REQ-048
Depends on:   SEAT-033
Size:         M
Scope:        `app/repositories/base.py`, `app/repositories/seat_repo.py`, `app/repositories/reservation_repo.py`
Done when:
  - Every row of the translation table in `08-error-logging.md` is implemented and has a test that injects the condition: `55P03` lock timeout → 409 `SEAT_TAKEN` with metric label `lock_timeout`, logged `warning`; `40P01` deadlock → 409 `SEAT_TAKEN`, logged `error`; `40001` serialization failure → retried once in-request then 409; `uq_seat_active_claim` → 409 `SEAT_TAKEN`, logged `error`; connection failure and pool-acquire timeout → 503 `DATABASE_UNAVAILABLE`; `query_canceled` → 503 `DATABASE_UNAVAILABLE`.
  - A deliberately held row lock plus a short `lock_timeout` produces a 409, never a 500, asserted end to end through the route.
  - No `asyncpg` exception type escapes `app/repositories/`; asserted by a static check that no module outside that package imports `asyncpg`.
  - The in-request retry is bounded to one attempt and is not applied to a domain decline.
Test:         `tests/integration/test_claim_errors.py`

### SEAT-036 — Money: `amount_paise` end to end and the float guard
Stage:        4
Covers:       REQ-060
Depends on:   SEAT-034
Size:         M
Scope:        `app/helpers/pricing.py`, `app/schemas/reservations.py`, `tests/unit/test_money.py`
Done when:
  - Every money field is declared as an integer in the schemas and stored as `BIGINT`; a schema receiving `250.00` returns 422 rather than coercing.
  - A static check fails the build if `float(`, `/`, or a `Decimal` conversion is applied to any identifier ending in `_paise`; the check is wired into `make lint`.
  - A reserve of the maximum permitted label count at an awkward per-seat price returns an `amount_paise` exactly equal to the integer sum, matched against the database row and against `reservation_seats`.
  - A round-trip through JSON preserves the exact integer at values above 2^53, asserted explicitly.
Test:         `tests/unit/test_money.py`, `tests/integration/test_reserve.py::test_amount_is_exact_integer_sum`

### SEAT-037 — Concurrency test harness
Stage:        4
Covers:       enabling — every concurrency ticket in this stage and Stage 5 depends on genuine overlap; a loop of sequential awaits tests nothing
Depends on:   SEAT-034
Size:         M
Scope:        `tests/concurrency/conftest.py`
Done when:
  - A barrier fixture releases N clients simultaneously; a self-test asserts the spread between first and last request start is under the configured threshold.
  - Each test gets its own show and its own principals, so tests are isolated without serializing the suite.
  - A helper mints N guest principals in bulk and returns their tokens.
  - A helper reads seat and reservation state through the repository layer, so no test contains SQL.
  - A helper asserts the Prometheus counter deltas for a test window, used by the cross-check step of `12-testing-and-burst.md`.
  - Parallelism, client count and limit values come from config, so the suite runs reduced in CI and full locally.
Test:         `tests/concurrency/conftest.py::test_barrier_releases_simultaneously` (the harness self-test)

### SEAT-038 — Integration: reserve contract suite
Stage:        4
Covers:       REQ-020, REQ-027, REQ-028, REQ-029
Depends on:   SEAT-036
Size:         M
Scope:        `tests/integration/test_reserve.py`
Done when:
  - One named test per status row of the reserve table in `06-apis.md`: 201, 409 `SEAT_TAKEN`, 409 `PER_USER_LIMIT`, 409 `SHOW_NOT_ON_SALE`, 404 `SHOW_NOT_FOUND`, 404 `SEAT_NOT_FOUND`, 422 for each validation cause, 401.
  - Every error response carries the envelope, the correct `code`, and a `request_id` matching the response header.
  - A sequential second reserve of a held seat returns 409 with `details.conflicts` listing exactly the held label.
  - A guest principal completes a reserve identically to a registered user, closing the guest path of `REQ-003`.
  - Every assertion is made against the database as well as the response, per the "count rows, do not read the response" rule in `12-testing-and-burst.md`.
Test:         `tests/integration/test_reserve.py`

### SEAT-039 — Concurrency: hot-seat storm
Stage:        4
Covers:       REQ-021
Depends on:   SEAT-037
Size:         M
Scope:        `tests/concurrency/test_double_sell.py`
Done when:
  - N barrier-synchronized clients target one seat; `status.count(201) == 1` and `status.count(409) == N - 1`, with N from config.
  - Every 409 body carries `code == "SEAT_TAKEN"`; zero responses are 5xx.
  - Exactly one `seats` row for that label has `held_by` set, and exactly one `reservation_seats` row for that seat has `released_at IS NULL`.
  - Zero winners fails the test as loudly as two winners — both assertions are explicit.
  - The counter cross-check passes: `reservations_created_total` and `reservations_declined_total{reason="seat_taken"}` deltas match the response distribution.
Test:         `tests/concurrency/test_double_sell.py::test_hot_seat_storm`

### SEAT-040 — Concurrency: multi-seat all-or-nothing under contention
Stage:        4
Covers:       REQ-022
Depends on:   SEAT-039
Size:         M
Scope:        `tests/concurrency/test_double_sell.py`
Done when:
  - Two clients request overlapping sets (`[A12,A13]` and `[A13,A14]`) simultaneously; exactly one returns 201 with both its seats.
  - The loser returns 409 listing `A13` in `details.conflicts` **and holds nothing** — the loser's non-contested seat is asserted `available` in the database, which is the defect this test exists to find.
  - The test is repeated with three and four overlapping clients and with the overlap on the first, middle and last label in sort order.
  - Zero 5xx and zero deadlocks; `reservations_declined_total{reason="deadlock"}` stays at zero.
Test:         `tests/concurrency/test_double_sell.py::test_multi_seat_loser_holds_nothing`

### SEAT-041 — Concurrency: per-user limit under parallel fire
Stage:        4
Covers:       REQ-023
Depends on:   SEAT-039
Size:         M
Scope:        `tests/concurrency/test_user_limit.py`
Done when:
  - One principal fires ten parallel single-seat reserves against a limit of four: exactly four 201s, six 409 `PER_USER_LIMIT`, zero 5xx.
  - The database shows exactly four seats held by that principal, counted through the repository.
  - Parameterized across the limit and parallelism values in config, including limit 1 and a parallelism above the seat count.
  - A multi-seat request that would cross the limit mid-request claims nothing, asserted on seat state.
  - A second principal reserving concurrently is unaffected, confirming the quota lock adds no cross-user serialization.
Test:         `tests/concurrency/test_user_limit.py::test_parallel_limit_breach`

### SEAT-042 — Idempotency under concurrency: duplicate keys, reuse and replayed declines
Stage:        4
Covers:       REQ-024, REQ-025, REQ-026
Depends on:   SEAT-039
Size:         L
Scope:        `tests/concurrency/test_idempotency.py`
Done when:
  - Two to ten clients fire an identical key and body together: exactly one reservation exists; every response is either the same 201 body or a 409 `IDEMPOTENCY_IN_PROGRESS`; no two different `reservation_id` values are ever returned for one key.
  - A sequential retry of a successful request returns the original status and body with `Idempotent-Replay: true`, and creates no second reservation and no seat movement, asserted on `seats.version`.
  - The same key with a mutated body returns 409 `IDEMPOTENCY_KEY_REUSED` and **no seat moved**, asserted on `seats.version` being unchanged.
  - A key whose original outcome was a 409 decline replays as the same 409.
  - The same key against a different show is treated as a distinct operation and succeeds.
  - A key left `in_progress` past the staleness window is reclaimed and the retry succeeds exactly once.
Test:         `tests/concurrency/test_idempotency.py`

### SEAT-043 — Negative controls against deliberately broken variants
Stage:        4
Covers:       enabling — a concurrency suite that cannot detect the bug it was written for is worse than none (`12-testing-and-burst.md`)
Depends on:   SEAT-040, SEAT-041, SEAT-042
Size:         L
Scope:        `tests/concurrency/test_negative_controls.py`
Done when:
  - Three broken variants are injectable by fixture: a read-then-write claim, a quota check with the row lock removed, and an idempotency claim inside T2 rather than its own transaction.
  - Against the read-then-write variant, `test_hot_seat_storm` **fails**; against the unlocked quota variant, `test_parallel_limit_breach` **fails**; against the late key claim, the concurrent-duplicate test **fails**. Each is asserted as an expected failure, so a control that stops detecting its bug breaks the build.
  - The variants are reachable only from this test module and cannot be enabled by configuration in a running service, asserted by a static check.
  - The module header states that a test never observed failing is not evidence.
Test:         `tests/concurrency/test_negative_controls.py`

### SEAT-044 — grill: Stage 4
Stage:        4
Covers:       enabling — the stage gate, and the review this service most depends on
Depends on:   SEAT-043
Size:         L
Scope:        review only; findings land as fixes on `SEAT-024` … `SEAT-043` or as `RISK-*`/`LEARN-*` entries in `mds/99-ledger.md`
Done when:
  - Every row of the "Threats to the argument" table in `04-concurrency-and-atomicity.md` is re-checked against the code as written, in writing.
  - Attack list covered explicitly: a `SELECT` reintroduced before a claim, `ORDER BY label` removed as a pointless sort, `SKIP LOCKED` leaking into the claim path, a quota lock taken after a seat lock on any path, a stored tally replacing the derived count, the key completion moved out of T2, a decline raised as an exception from the repository, an expiry boundary computed in Python rather than from `now()`, a second copy of the effective-status expression, a 5xx reachable on the reserve path.
  - The hot-seat storm, multi-seat, limit, idempotency and negative-control suites are run by the reviewer, not taken on report.
  - Every finding is fixed, refuted in writing, or recorded as `RISK-*`; durable discoveries are appended as `LEARN-*` and folded into the `concurrency-correctness` skill per the self-enhancement rule.
Test:         the Stage 4 concurrency suite, re-run by the reviewer

---

## Stage 5 — Lifecycle

Release must never resurrect. Every statement in this stage is a guarded update predicated on current ownership, for the same reason the claim is: the decision and the effect must be one operation.

### SEAT-045 — Confirm and cancel: guarded statements, service and routes
Stage:        5
Covers:       REQ-030, REQ-031, REQ-032, REQ-034
Depends on:   SEAT-044
Size:         L
Scope:        `app/repositories/seat_repo.py` (`confirm_for_reservation`, `release_for_reservation`), `app/repositories/reservation_repo.py` (`mark_confirmed`, `mark_cancelled`), `app/services/reservation_service.py`, `app/api/routes/reservations.py`, `app/schemas/reservations.py`
Done when:
  - Both seat statements are guarded on `reservation_id = $id` and the current status, exactly as written in `04-concurrency-and-atomicity.md`; a seat whose `reservation_id` now points elsewhere matches zero rows.
  - `POST /reservations/{id}/confirm` by the owner returns 200, sets seats and reservation `confirmed`, and clears `hold_expires_at`; `ck_seats_hold_coherent` is satisfied, asserted by the constraint not firing on any path.
  - Confirming an already-confirmed reservation returns 200 with the same body. Confirming an expired reservation returns 409 `RESERVATION_EXPIRED` with `details.status`; a cancelled one returns 409 `RESERVATION_CANCELLED`; one whose seat has lapsed and been re-claimed returns 409 `SEAT_TAKEN` and leaves the new owner's seat untouched.
  - `POST /reservations/{id}/cancel` by the owner returns 200, releases held seats to `available`, sets `cancelled_at`, and closes `reservation_seats` rows with `released_at`. Cancelling twice is idempotent; cancelling a confirmed reservation returns 409 `RESERVATION_CONFIRMED`.
  - Ownership is a `WHERE` clause via `get_owned`; a non-owner — including an admin — receives 404 `RESERVATION_NOT_FOUND` with no state change, and the latency of a non-owner request is indistinguishable from an unknown id.
Test:         `tests/concurrency/test_lifecycle.py` (added by `SEAT-048`), `tests/integration/test_reservations.py`

### SEAT-046 — Reservation reads
Stage:        5
Covers:       REQ-036
Depends on:   SEAT-045
Size:         S
Scope:        `app/repositories/reservation_repo.py` (`list_for_user`), `app/api/routes/reservations.py`, `app/schemas/reservations.py`
Done when:
  - `GET /reservations` returns only the calling principal's reservations, keyset-paginated, and `GET /reservations/{id}` returns 404 for a reservation owned by anyone else.
  - A reservation whose opt-in hold has lapsed reads as expired, derived at read time — there is no worker, so stored status is not relied on (ADR-017).
  - Ownership is a `WHERE` clause, not a post-fetch comparison; no unfiltered get-by-id exists in the repository.
Test:         `tests/integration/test_reservations.py::test_reads_are_owner_scoped`, `::test_lapsed_hold_reads_as_expired`

### SEAT-047 — Integration: lifecycle, ownership and expiry reporting
Stage:        5
Covers:       REQ-012, REQ-030, REQ-031, REQ-032, REQ-035, REQ-036
Depends on:   SEAT-046
Size:         L
Scope:        `tests/integration/test_reservations.py`, `tests/integration/test_authz.py`, `tests/integration/test_shows.py`
Done when:
  - A hold that has lapsed but not been swept reports `available` in `GET /shows/{show_id}`, and a claim on it succeeds in the same test — closing the `REQ-012` clause deferred from `SEAT-021`.
  - A seat released by cancel, and a seat released by sweep, are each re-reserved by a different principal; the resulting reservation is asserted field-for-field indistinguishable from a first booking, with no residual `held_by`, `reservation_id` or `hold_expires_at`.
  - Cross-principal confirm and cancel each return 404, added to the authorization probe suite so `REQ-032` is traced there as well.
  - A guest reserves, confirms and cancels successfully, then upgrades and still owns the same reservations — closing the deferred clause of `SEAT-017`.
  - Every lifecycle transition is asserted against the database, not only the response.
Test:         the files named in Scope

### SEAT-048 — Concurrency: claim racing expiry
Stage:        5
Covers:       REQ-033, REQ-034
Depends on:   SEAT-047
Size:         M
Scope:        `tests/concurrency/test_lifecycle.py`
Done when:
  - A hold about to lapse is raced by a competing claim; the seat ends owned by exactly one principal, asserted on `seats` and on the single active `reservation_seats` row.
  - The original holder's `confirm` either succeeds (it won) or returns 409 (it lost), and never takes the seat from a new owner — asserted by comparing `held_by` before and after the losing confirm.
  - A cancel of a reservation whose seat has moved on affects zero rows and leaves the new owner's seat untouched.
  - A cancel raced against a competing claim on a lapsing hold produces no double-release: exactly one active `reservation_seats` row per contested seat, and `reservations_cancelled_total` never exceeds the number of reservations.
  - Zero 5xx across every variant.
Test:         `tests/concurrency/test_lifecycle.py::test_claim_races_expiry`, `::test_release_never_resurrects`

### SEAT-049 — grill: Stage 5
Stage:        5
Covers:       enabling — stage gate
Depends on:   SEAT-048
Size:         S
Scope:        review only
Done when:
  - Attack list covered in writing: a release that is not predicated on current ownership, a confirm that can reclaim a re-sold seat, a 403 leaking existence to a non-owner, an admin implicitly overriding ownership, a cancel path that writes an incoherent seat row, a superseded `reservation_seats` row left open so the next legitimate claim trips `uq_seat_active_claim` (ADR-019), and the two statements of that closure folded into one CTE despite LEARN-009.
  - Every finding fixed, refuted in writing, or recorded as `RISK-*`.
Test:         n/a

---

## Stage 6 — Observability

Metrics are not decoration here: `reservations_declined_total{reason}` is how a burst is read, `unhandled_exceptions_total` is the direct measurement of `REQ-048`, and the audit path must be provably incapable of adding latency to a booking.

### SEAT-050 — `core/metrics.py`, `MetricsMiddleware`, domain counters and the gauge refresher
Stage:        6
Covers:       REQ-042, REQ-043
Depends on:   SEAT-049
Size:         L
Scope:        `app/core/metrics.py`, `app/middleware/metrics.py`, `app/api/routes/metrics.py`, `app/services/reservation_service.py`, `app/services/show_service.py`, `app/workers/` (gauge refresher task), `app/main.py`
Done when:
  - Every metric in the three catalogue tables of `10-observability.md` is declared exactly once in `core/metrics.py` and present on `GET /metrics`; a test asserts the declared set equals the catalogued set, so a catalogued metric that is never declared fails the build.
  - `reservations_declined_total` is labelled by `reason` over exactly the eight documented values; a counter incremented with an undocumented reason raises.
  - Domain counters are incremented in the service layer, not in middleware; a static check asserts `app/middleware/` does not import the domain counters.
  - HTTP metrics are labelled by route **template**, method and status only. A test issuing requests against 50 distinct show ids asserts the series count does not grow, and that no label value anywhere contains a user id, seat label, idempotency key or concrete path.
  - The gauge refresher publishes per-show seat gauges on the configured interval using the same `db/sql.py` counts query the API uses, capped to `GAUGE_MAX_SHOWS` recently active shows; exceeding the cap evicts rather than growing.
  - After a quiet period following a burst, `seats_available + seats_held + seats_confirmed == seats_total` per show, and the exact counters agree with the database to the unit.
Test:         `tests/integration/test_ops.py::test_metrics_catalogue_is_complete`, `::test_no_unbounded_label`, `tests/concurrency/test_reconciliation.py::test_counters_agree_with_database`

### SEAT-051 — Audit path: migration, repository, middleware and batched writer
Stage:        6
Covers:       REQ-046
Depends on:   SEAT-050
Size:         L
Scope:        `app/db/migrations/` (one revision), `app/repositories/audit_repo.py`, `app/middleware/audit.py`, `app/services/audit_service.py`, `app/workers/audit_writer.py`, `app/main.py`
Done when:
  - `audit_log` exists with every column and index from `03-data-model.md` and **no foreign keys**, asserted by querying the catalogue.
  - `AuditMiddleware` builds one record per request and enqueues with `put_nowait`; a `QueueFull` increments `audit_records_dropped_total` and logs `audit_queue_saturated` at `warning`. The middleware never awaits the queue and never reads the request body — asserted by a test that sends a large body and measures no additional latency.
  - `show_id` comes from path parameters and `seat_labels` from a context var set by the service, not from body parsing.
  - The writer drains up to `AUDIT_BATCH_SIZE` or `AUDIT_FLUSH_INTERVAL_MS` and inserts in one multi-row statement on a **dedicated connection outside the request pool**; a test asserts pool in-use does not rise when the writer flushes.
  - With the queue deliberately sized to 1 and 200 requests fired, every request still succeeds with its correct status, `audit_records_dropped_total` is greater than zero, and request latency is within the no-audit baseline tolerance.
  - Graceful shutdown drains the queue within a bounded timeout; a test asserts the last enqueued record is present in `audit_log` after shutdown.
  - Every audited row carries the request's `request_id`, matching the response header.
Test:         `tests/integration/test_ops.py::test_audit_saturation_drops_without_degrading`, `::test_shutdown_flushes_audit_queue`

### SEAT-052 — `RateLimitMiddleware` with per-class ceilings
Stage:        6
Covers:       REQ-047
Depends on:   SEAT-051
Size:         L
Scope:        `app/middleware/rate_limit.py`, `app/core/config.py`, `app/api/routes/health.py`
Done when:
  - A monotonic-clock token bucket per `(principal_or_ip, route_class)` sits in a bounded LRU; a flood of 100,000 distinct principals does not grow memory without limit, asserted by a bounded bucket count.
  - Keying is by **principal** wherever a token is present and by IP only on pre-authentication routes; a test fires 500 distinct principals from one IP at the reserve path and asserts **zero** 429s — the property that keeps a legitimate stampede from being throttled.
  - Every ceiling in the route-class table of `07-middleware.md` is read from an environment variable; changing one and restarting changes behaviour with no code edit, and a test asserts no numeric ceiling literal exists in the middleware.
  - A 429 carries `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, the standard envelope with code `RATE_LIMITED`, and the request id; `rate_limited_total{route_class}` increments.
  - `/healthz`, `/readyz` and `/metrics` are exempt.
  - `RATE_LIMIT_ENABLED=false` disables limiting entirely, is logged at startup, and is reported on `/readyz`; a test asserts a service running unlimited cannot do so silently.
  - A throttled request still appears in the access log and the latency histogram, confirming the limiter sits inside the metrics layer.
Test:         `tests/integration/test_ops.py::test_distinct_principals_are_not_throttled`, `::test_rate_limit_envelope_and_headers`

### SEAT-053 — Integration: operational suite
Stage:        6
Covers:       REQ-042, REQ-043, REQ-046, REQ-047
Depends on:   SEAT-052
Size:         L
Scope:        `tests/integration/test_ops.py`, `tests/concurrency/test_reconciliation.py`
Done when:
  - `/metrics` parses as valid Prometheus text format with a real parser, not a substring check.
  - The counter cross-check runs after each concurrency suite: API state, database rows and counters agree; a disagreement fails the suite even when the API looks correct.
  - Restart mid-load is tested: no double-sell, no key left permanently `in_progress`, invariant intact afterwards.
  - A named test per requirement id for `REQ-042`, `REQ-043`, `REQ-046`, `REQ-047`, each citing the id in its docstring.
Test:         the files named in Scope

### SEAT-054 — grill: Stage 6
Stage:        6
Covers:       enabling — stage gate
Depends on:   SEAT-053
Size:         S
Scope:        review only
Done when:
  - Attack list covered in writing: an unbounded metric label, a gauge computed on the hot path, a counter incremented in middleware that cannot know the outcome, an audit write awaited in a request, the audit writer sharing the request pool, a drop that is silent, a limiter keyed by IP on the reserve path, a ceiling hardcoded, a 429 with no request id.
  - `RISK-004` (gauge lag within one refresh interval) confirmed as still accepted and still bounded by the configured interval.
  - Every finding fixed, refuted in writing, or recorded as `RISK-*`.
Test:         n/a

---

## Stage 7 — Burst and hardening

This is the stage that produces evidence; everything before it is a claim. The burst script is a gate, not a report — it exits non-zero on any violation.

### SEAT-055 — Burst harness, configuration and phases 0–2
Stage:        7 — **startable alongside Stage 0**; it codes against the HTTP contract in `06-apis.md`, which is already written, and needs no running service until SEAT-058
Covers:       REQ-049
Depends on:   SEAT-001
Size:         L
Scope:        `burst/burst.py`, `burst/config.py`, `burst/client.py`
Done when:
  - Every parameter is settable by flag or environment variable — base URL, seat count, principal count, concurrency, hot-seat count, retry share, limit-probe size; a test asserts no value is hardcoded.
  - Phase 0 polls `/readyz` until ready with a bounded timeout before anything is measured, so a cold start is never recorded as load.
  - Phase 1 authenticates as admin, creates a **fresh** show, and mints the configured number of principals as guests by default; the script touches no pre-existing show, asserted by comparing the show list before and after.
  - Phase 2 captures `/metrics` and `GET /shows/{show_id}` as a baseline.
  - The HTTP client uses a bounded connection pool with keep-alive; a run at full concurrency exhausts no local file descriptors, and a client-side failure is reported as a client failure, never counted as a server error.
Test:         `burst/burst.py --phases 0,1,2` against a local instance, asserted in CI at reduced scale

### SEAT-056 — Burst phases 3–7
Stage:        7
Covers:       REQ-005, REQ-021, REQ-023, REQ-024, REQ-025
Depends on:   SEAT-055
Size:         L
Scope:        `burst/burst.py`, `burst/phases.py`
Done when:
  - Phase 3 stampede: all principals reserve random seats at the configured concurrency.
  - Phase 4 hot-seat storm: all principals target one seat, barrier-synchronized, repeated for the configured number of hot seats; a per-hot-seat winner count is recorded and any count other than one is a violation.
  - Phase 5 idempotent retries: the configured share replays an exact request with the same key, a smaller share replays the same key with a mutated body; replays are counted separately from declines.
  - Phase 6 limit probe: one principal fires more parallel reserves than the limit permits.
  - Phase 7 spoof probe: a reserve carrying another principal's id in the body; the script asserts the resulting reservation belongs to the token's subject and records the outcome as a pass/fail line.
  - 429 is tallied in its own column, never folded into 409, so a throttling artifact can never be mistaken for a seat decline.
Test:         `burst/burst.py --phases 3,4,5,6,7`, asserted in CI at reduced scale

### SEAT-057 — Burst phases 8–9, output table, exit contract and wrappers
Stage:        7
Covers:       REQ-013, REQ-035, REQ-043
Depends on:   SEAT-056
Size:         L
Scope:        `burst/burst.py`, `burst/report.py`, `burst.sh`, `Makefile`
Done when:
  - Phase 8 lifecycle: a share of winners confirm and a share cancel; cancelled seats are re-reserved successfully, proving clean re-bookability.
  - Phase 9 reconcile: `GET /shows/{show_id}` and `/metrics` are re-read; the invariant and metric agreement are each checked and printed.
  - Output matches the format in `12-testing-and-burst.md`: the per-phase request table with 201/200/409/422/429/5xx columns and p50/p99, declines by reason, per-hot-seat winner counts, invariant sample ratio, final counts, and the metric, spoof and 5xx verdict lines.
  - Exit is non-zero on **any** of: an invariant violation, any 5xx, a hot seat with a winner count other than one, a metric disagreement, or a successful identity spoof. Each condition is proven by a self-test that injects it and asserts the non-zero exit.
  - `./burst.sh <BASE_URL>` and `make burst` both run the full script against any base URL, local or live.
Test:         `burst/tests/test_exit_contract.py` (injects each violation and asserts non-zero exit)

### SEAT-058 — Reconciliation under load: poller, test and pool tuning
Stage:        7
Covers:       REQ-013, REQ-048
Depends on:   SEAT-057, SEAT-044
Size:         L
Scope:        `burst/poller.py`, `tests/concurrency/test_reconciliation.py`, `app/core/config.py`, `render.yaml`
Done when:
  - A poller samples `GET /shows/{show_id}` continuously throughout phases 3 to 8 and asserts `available + held + confirmed == total_seats` on **every** sample; the sample count and pass count are printed and must be equal.
  - `tests/concurrency/test_reconciliation.py` runs the same poller against local load, so the property is covered in CI and not only by the live burst.
  - Pool size, acquire timeout, `statement_timeout` and `lock_timeout` are set from the arithmetic in `11-scalability.md` against the deployed database's actual ceiling, with the chosen numbers and the measured queue drain recorded in the ledger.
  - `db_pool_waiting` is observed at zero, or with a bounded excursion that does not produce a 503, across a full-scale run.
  - A deliberately drifted effective-status expression makes this test fail, confirming it detects the failure mode it exists for.
Test:         `tests/concurrency/test_reconciliation.py`, plus the poller line in the burst output

### SEAT-059 — Full-scale run against the live URL
Stage:        7
Covers:       REQ-013, REQ-021, REQ-048
Depends on:   SEAT-058, SEAT-054
Size:         M
Scope:        execution and evidence capture; configuration fixes only, no new code
Done when:
  - The burst runs against the deployed URL at **full** configured scale, not reduced, and exits zero.
  - Exactly one winner per hot seat; zero 5xx; the invariant held on every sample; metrics agree with the API; the spoof was ignored.
  - `unhandled_exceptions_total` is zero and `audit_records_dropped_total` is zero for the run.
  - The captured output is recorded, and any configuration change made to achieve it is committed rather than applied by hand.
  - `/readyz`, `/healthz` and `/metrics` are verified healthy after the run; the live URL is left working.
Test:         `./burst.sh <live URL>` — the run itself is the evidence

### SEAT-060 — grill: Stage 7
Stage:        7
Covers:       enabling — stage gate
Depends on:   SEAT-059
Size:         M
Scope:        review only
Done when:
  - Attack list covered in writing: a burst that measures a cold start as load, a script that prints PASS without a distribution, 429 counted as 409, a hot-seat phase without a barrier, reconciliation sampled only after the run, a client-side failure reported as a server error, a reduced-scale run presented as full-scale, an exit code that stays zero on a violation.
  - The reviewer re-runs the burst against the live URL independently.
  - Every finding fixed, refuted in writing, or recorded as `RISK-*`.
Test:         an independent `./burst.sh` run against the live URL

---

## Stage 8 — Documentation

No `grill` ticket: this stage has no correctness surface of its own. The equivalent check is `SEAT-063`, which verifies the documented commands on a genuinely fresh clone and reconciles every claim in the documentation against the implementation.

### SEAT-061 — README
Stage:        8
Covers:       REQ-050
Depends on:   SEAT-059
Size:         M
Scope:        `README.md`
Done when:
  - All nine contents items listed in `13-deployment.md` are present, including the live URL, the one-command local run, the no-Docker Homebrew Postgres path, the burst script with flags and annotated sample output, and the free-tier cold-start caveat stated plainly.
  - The auth quickstart is copy-pasteable `curl`: guest token, admin show creation, reserve, confirm — each command run as written and its real output pasted in.
  - The documented semantics section states all-or-nothing multi-seat, hold-then-confirm, and idempotency behaviour including same-key-different-body.
  - Every environment variable in the `13-deployment.md` table is listed, with secrets shown as placeholders.
Test:         `SEAT-063` executes every command in the README verbatim

### SEAT-062 — `WRITEUP.md`
Stage:        8
Covers:       enabling — the design record the stage plan requires; no requirement names it
Depends on:   SEAT-061
Size:         M
Scope:        `WRITEUP.md`
Done when:
  - All seven topics from `14-stage-plan.md` are covered: the atomic mechanism and why it is race-free, multi-seat deadlock avoidance, idempotency storage and enforcement, holds and expiry, consistency versus availability under partition, what would page someone at 2am, and what would come next.
  - The claim statements quoted are byte-identical to the ones in `app/repositories/seat_repo.py`; a check diffs them.
  - Every `ADR-*` and `RISK-*` referenced exists in `mds/99-ledger.md`.
  - The `READ COMMITTED` re-evaluation argument cites the `LEARN-002` probe results rather than asserting the behaviour.
Test:         a documentation check that diffs the quoted SQL against the repository module

### SEAT-063 — Final clean-clone verification
Stage:        8
Covers:       REQ-050
Depends on:   SEAT-062
Size:         S
Scope:        verification only; fixes land on `SEAT-055` … `SEAT-062`
Done when:
  - A clone into a fresh directory, with no cached virtualenv, image layer, or local env file, builds and runs via the documented command with no undocumented manual step.
  - The same image that was verified locally is the one the platform deploys, confirmed by digest.
  - Every command in the README is executed verbatim and produces the documented result.
  - Every factual claim in `README.md` and `WRITEUP.md` is reconciled against the implementation; a discrepancy is resolved in whichever artifact is wrong and recorded, never left implicit.
Test:         the CI `container-smoke` job plus a manual fresh-directory run, both recorded

---

## Stage 9 — Monitoring view (optional)

**Entry condition: Stages 0–8 fully closed.** This is additive and must not be started while any correctness gate is open. Both tickets are strictly read-only and strictly off the request path.

### SEAT-064 — Audit aggregate read path
Stage:        9
Covers:       enabling — reads `audit_log`, which `REQ-046` already requires; adds no requirement of its own
Depends on:   SEAT-063
Size:         M
Scope:        `app/repositories/audit_repo.py`, `app/services/audit_service.py`, `app/api/routes/admin.py`
Done when:
  - Aggregate queries exist for request rate, status distribution, declines by `outcome_code`, and recent failures, each served by one of the existing `audit_log` indexes, confirmed by `EXPLAIN`.
  - Every query is bounded by a configured time window and row limit; an unbounded scan is impossible by construction.
  - The routes are admin-only via `require_admin` and return 403 for a `user` or `guest` token.
  - No aggregate query runs on a request-path connection — a test asserts the request pool's in-use count is unaffected while an aggregate is served.
Test:         `tests/integration/test_admin_audit.py`

### SEAT-065 — Read-only monitoring view
Stage:        9
Covers:       enabling — operator-facing surface over `REQ-046` data
Depends on:   SEAT-064
Size:         M
Scope:        `app/api/routes/admin.py`, a single server-rendered template or static page
Done when:
  - The page shows request rate, status distribution, decline reasons by code, and recent failures, each failure row linking to its `request_id`.
  - It performs no write of any kind; a test asserts the handler issues only `SELECT` statements.
  - It is reachable only by an admin principal and is absent from the public route table unless listed there in the same commit.
  - Rendering the page with an empty `audit_log` produces a valid page, not an error.
Test:         `tests/integration/test_admin_audit.py::test_monitoring_view_is_read_only`

---

## Added after the Stage 0 review

Appended rather than renumbered, so existing ids stay stable. Both arise from decisions taken after the board was first written.

### SEAT-066 — Framework failures answer inside the envelope
Stage:        0
Covers:       REQ-045 — "every failure uses the envelope" is not satisfied while unmatched routes bypass it
Depends on:   SEAT-004
Size:         S
Scope:        `app/core/error_codes.py`, `app/main.py`, `app/middleware/` (catch-all boundary)
Done when:
  - `GET /nope` returns 404 in the standard envelope with code `ROUTE_NOT_FOUND` and a `request_id`, not Starlette's `{"detail":"Not Found"}` (ADR-023).
  - A wrong method on a known path returns 405 in the envelope with `METHOD_NOT_ALLOWED`.
  - An unhandled exception produces **exactly one** log line carrying the request id and a stack trace — the catch-all is a middleware boundary immediately inside the request context, not only an exception handler, because `ServerErrorMiddleware` re-raises after responding and logs again outside the context (ADR-024, LEARN-010).
  - `unhandled_exceptions_total` increments once per fault, not twice.
  - The registry test from SEAT-003 passes with the two new codes present.
Test:         `tests/integration/test_errors.py::test_unmatched_route_uses_envelope`, `::test_bad_method_uses_envelope`, `::test_unhandled_exception_logs_once_with_request_id`

### SEAT-067 — `GET /shows`: paginated catalogue without counts
Stage:        3
Covers:       REQ-014
Depends on:   SEAT-021
Size:         M
Scope:        `app/repositories/show_repo.py` (`list_shows`), `app/services/show_service.py`, `app/api/routes/shows.py`, `app/schemas/shows.py`, `app/helpers/pagination.py`, `app/utils/cursor.py`
Done when:
  - `GET /shows` returns id, name, `event_kind`, status, `price_paise`, `total_seats` and the sale window, keyset-paginated on the immutable `(created_at, id)`, with `limit` bounded by configuration.
  - **No availability counts in the list response, and the query never scans `seats`** — asserted by a test that creates several large shows and checks the plan touches no seat rows. Exact availability remains the job of `GET /shows/{id}`.
  - `status` and `event_kind` filters work, and the endpoint is reachable unauthenticated.
  - Paging through a catalogue that is being booked concurrently neither skips nor duplicates a show, because the sort key cannot change.
Test:         `tests/integration/test_shows.py::test_list_is_keyset_paginated`, `::test_list_does_not_scan_seats`, `::test_list_is_stable_under_concurrent_booking`

---

## Critical path

The shortest chain from nothing to a live service that holds every invariant under a full-scale burst. Every ticket on it is a blocker: slipping one slips the evidence in `SEAT-059`.

```
001 → 002 → 003 → 004 → 005 → 008 → 009 → 010          foundation + live deploy
    → 013 → 014 → 015 → 016                            principals, incl. guests the burst mints
    → 019 → 020 → 021                                  schema, show creation, the counts read
    → 024 → 025 → 026 → 028 → 029 → 030 → 031
    → 032 → 033 → 034 → 035                            the atomic claim end to end
    → 037 → 039 → 040 → 041 → 042 → 043 → 044          the evidence that it is correct
    → 045 → 047                                        confirm/cancel, for burst phase 8
    → 050                                              metrics, for burst phase 9 agreement
    → 055 → 056 → 057 → 058 → 059                      the full-scale run against the live URL
```

**On the path and non-negotiable:** `SEAT-010` (a live URL from Stage 1; discovering a platform problem at Stage 6 costs a day), `SEAT-019` (the two seat constraints every later proof rests on), `SEAT-025` and `SEAT-026` (the claim), `SEAT-028` (the quota lock), `SEAT-030`–`SEAT-031` (the key lifecycle), `SEAT-033` (the two-transaction boundary), `SEAT-035` (the only thing standing between a lock timeout and a 5xx), `SEAT-043` (without the negative controls, the concurrency suite is an untested assertion), `SEAT-058` (pool arithmetic is where zero-5xx is won or lost).

**What can slip without putting the burst at risk:** `SEAT-006` and `SEAT-022` (valuable regression cover, not prerequisites),  `SEAT-027` (the probes re-prove what `SEAT-039` also proves, so they can follow it), `SEAT-036` (money exactness is independent of contention), `SEAT-046` (reservation reads are not on the booking path), `SEAT-051` and `SEAT-052` (audit and rate limiting are protective, and no invariant depends on either), Stage 8 entirely, Stage 9 entirely.

**What must never slip past its stage gate:** `SEAT-044`. Stage 4's `grill` is the review the service is judged on, and no Stage 5 ticket opens before it closes.

---

## Coverage

Every `REQ-*` in [01-requirements.md](01-requirements.md) maps to at least one ticket. Implementation tickets and the ticket that proves them are both listed; the proving ticket is in **bold**.

| REQ | Tickets |
|---|---|
| REQ-001 | 015, **017** |
| REQ-002 | 013, 015, **017** |
| REQ-003 | 016, **017**, 038 |
| REQ-004 | 016, **017**, 047 |
| REQ-005 | 014, 034, **017**, 056 |
| REQ-006 | 013, 014, **017** |
| REQ-007 | 014, 020, **017** |
| REQ-008 | 015, **017** |
| REQ-010 | 020, **022** |
| REQ-011 | 020, **022** |
| REQ-012 | 019, 021, **022**, **047** |
| REQ-013 | 019, 021, **022**, **058**, 059 |
| REQ-014 | 021, **022** |
| REQ-020 | 025, 026, 032, 033, 034, **038** |
| REQ-021 | 024, 025, 027, **039**, 059 |
| REQ-022 | 026, 033, **040** |
| REQ-023 | 028, 033, **041**, 056 |
| REQ-024 | 030, 031, **042** |
| REQ-025 | 029, 031, **042**, 056 |
| REQ-026 | 030, 031, **042** |
| REQ-027 | 034, **038** |
| REQ-028 | 034, **038** |
| REQ-029 | 033, **038** |
| REQ-030 | 045, **047** |
| REQ-031 | 045, **047** |
| REQ-032 | 045, **047** |
| REQ-033 | 027, 046, **048** |
| REQ-034 | 045, **048** |
| REQ-035 | **047**, 057 |
| REQ-036 | 046, **047** |
| REQ-040 | 004, **006**, **011** |
| REQ-041 | 009, **011** |
| REQ-042 | 050, **053** |
| REQ-043 | 050, **053**, 057 |
| REQ-044 | 004, **006** |
| REQ-045 | 003, **006** |
| REQ-046 | 051, **053** |
| REQ-047 | 052, **053** |
| REQ-048 | 003, 035, **058**, **059** |
| REQ-049 | 010, **011**, 055 |
| REQ-050 | 002, 005, 010, 061, **063** |
| REQ-060 | 020, **022**, 036 |

**Unplaced requirements: none.** All 42 requirements are covered, and each has at least one ticket whose `Done when` checks are observable.

Tickets covering no requirement, with their justification, are: `001` (environment prerequisites), `007`, `012`, `018`, `023`, `044`, `049`, `054`, `060` (the stage gates themselves), `008`, `024` (schema and pooling that requirements assume rather than name), `037` (the harness every concurrency test needs), `043` (the credibility of the concurrency suite), `062` (the design record), `064`, `065` (the optional read-only view).

---

## Gaps found

Decomposing surfaced ten places where `mds/01-requirements.md` is silent on behaviour the rest of the design specifies, plus one genuine conflict. None of these is added to a ticket's scope unilaterally — each needs a decision from the requirements owner, and the recommendation is stated so none of them blocks a stage.

| # | Gap | Recommendation |
|---|---|---|
| 1 | **Conflict: REQ-048 versus `11-scalability.md`.** REQ-048 requires "zero 5xx" across a sustained burst; `11-scalability.md` names 503 `DATABASE_UNAVAILABLE` on pool-acquire timeout as "the single legitimate 5xx". As written, a correct implementation can fail REQ-048 under pool pressure. | Amend REQ-048 to: zero 5xx on every domain path, and 503 `DATABASE_UNAVAILABLE` only when the database is genuinely unreachable, with zero occurrences required during the burst. `SEAT-058` already measures it; the requirement should say so rather than leaving the exception implicit. |
| 2 | **Confirm after a lapsed-and-re-claimed hold.** REQ-030 covers expired and cancelled; `06-apis.md` adds 409 `SEAT_TAKEN` for the hold that lapsed and whose seat was re-claimed. No requirement states that outcome. | Add it as an acceptance clause on REQ-030. It is the `hold hijack after expiry` threat in `05-auth-and-rbac.md` and is already tested by `SEAT-048`. |
| 3 | **Cancel of a confirmed reservation.** REQ-031 states only that a repeat cancel is idempotent; `06-apis.md` defines 409 `RESERVATION_CONFIRMED`. | Add the status and code to REQ-031's acceptance criteria. |
| 4 | **Stale idempotency-key reclaim and retention.** `04-concurrency-and-atomicity.md` specifies both; no requirement does, so `SEAT-030` carries them as enabling work with no traced acceptance criterion. | Add a requirement: a key `in_progress` beyond the configured staleness window is reclaimable exactly once, and keys are purged after `IDEMPOTENCY_RETENTION_HOURS`. Without it, one crash poisoning a key forever is an untraced failure mode. |
| 5 | **Idempotency key scope.** No requirement says the same key against a different show is a distinct operation, though `scope = reserve:{show_id}` is in the schema and in `SEAT-031`. | Add the clause to REQ-024, since a client reusing one key across two shows is a realistic retry pattern and the wrong answer would be a wrong replay. |
| 6 | **Per-request `hold_ttl_seconds` clamping.** `06-apis.md` accepts it and clamps it to the show's maximum; no requirement defines the clamp or the behaviour when the value exceeds it. | Add to REQ-020: an over-maximum value is clamped silently, or rejected 422 — pick one. Recommendation: clamp, matching `06-apis.md`, and state it. |
| 7 | **Operational metric set incomplete in REQ-042.** REQ-042 lists five metrics; `10-observability.md` catalogues `unhandled_exceptions_total`, `db_pool_waiting` and `seat_claim_lock_wait_seconds`, and three of the nine alerts depend on them. | Extend REQ-042's list to the full catalogue, or add a second requirement for the operational metrics. `unhandled_exceptions_total` especially: it is the direct measurement of REQ-048 and is currently required by no requirement. |
| 8 | **Graceful-shutdown audit flush.** Required by `10-observability.md` and tested per `12-testing-and-burst.md`; REQ-046 does not mention it. | Add to REQ-046: a graceful shutdown drains the queue within a bounded timeout. |
| 9 | **Admin bootstrap has no requirement.** Open question 1 carries a working default and `SEAT-015` implements it, but startup behaviour that creates a privileged principal should be a traced requirement rather than an answered question. | Promote open question 1 to a requirement: idempotent, created only when absent, logged loudly, and absent credentials proceed without an admin. |
| 10 | **`GET /auth/me` has no requirement.** It appears in the route table and the permission matrix only. | Add a one-line requirement, or fold it into REQ-005 as the canonical check that the token subject is the acting principal. |
| 11 | **Guest token lifetime versus hold TTL.** Open question 5 carries the default and `SEAT-016` enforces it at startup, but a guest whose token expires before their hold cannot confirm — a user-visible failure with no traced requirement. | Promote to a requirement on REQ-003: a guest token's lifetime exceeds `hold_ttl_seconds` plus the configured margin, validated at startup. |

Nothing in this list blocks Stage 0. Items 1, 2, 3 and 7 should be resolved before the stages that cover them close — 1 before Stage 7, 2 and 3 before Stage 5, 7 before Stage 6.
