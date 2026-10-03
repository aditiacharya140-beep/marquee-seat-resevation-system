# Stage plan

Build order is chosen by **risk**, not by layer. The two things that sink this service are an unproven atomic claim and a deploy that does not come up, so both are proven on real infrastructure before any breadth is added. A stage that is cheap but low-risk waits.

## Gate

A stage closes only when all five hold. From the `dev-team` skill, repeated here because it is the thing most likely to be skipped under time pressure:

1. Acceptance criteria pass, each traced to a `REQ-*` id.
2. Tests exist that fail if the stage regresses.
3. `grill` has reviewed; every finding is fixed, refuted in writing, or logged as `RISK-*`.
4. Work is committed incrementally.
5. The deployment is still healthy. From Stage 1 onward, the live URL is never left broken.

Do not open stage N+1 over an open stage N. If a later stage proves an earlier decision wrong, reopen it explicitly in the ledger rather than patching around it.

---

## Stage 0 — Foundation

Repository skeleton per the layout in [02-architecture.md](02-architecture.md). `core/config.py` with typed settings and startup validation. `core/logging.py` emitting JSON. `core/errors.py` and `error_codes.py`. `core/context.py`. `RequestContextMiddleware` and the access log. `/healthz`. Dockerfile, `.dockerignore`, `docker-compose.yml`. CI running lint, types, and a container smoke test.

**Exit:** container builds from a clean checkout; `/healthz` 200; a request's log line carries an adopted or minted request id and the response echoes it; a missing required secret refuses to boot.
**Covers:** REQ-040, REQ-044, REQ-045, REQ-050.
**Why first:** every later stage depends on this, and the clean-checkout build is the single most common failure mode.

## Stage 1 — Database and first deploy

Postgres locally (Homebrew or compose — neither is currently installed on this machine). Alembic. Migration for `users`. Pool with session guards. `/readyz` with a real query, failing closed. `render.yaml`. **Deploy.**

**Exit:** live URL reachable; `/readyz` 503 with the database stopped and 200 with it up; migrations apply on a fresh database; cold start reaches ready.
**Covers:** REQ-041, REQ-049.
**Why here:** deploying an almost-empty service proves the pipeline while it is still trivial to debug. Discovering a platform problem at Stage 6 costs a day; discovering it here costs minutes.

## Stage 2 — Auth and RBAC

Argon2 hashing in a bounded executor. JWT encode/decode with `typ` checks. Register, login, guest, upgrade, refresh, me. `Depends` providers: `get_current_user`, `require_user`, `require_admin`. Idempotent admin bootstrap. Startup validation that guest token lifetime exceeds hold TTL.

**Exit:** every flow works end to end; the full authorization probe suite passes, including spoofed identity, cross-principal access, refresh-as-access, tampered tokens; guest upgrade preserves `user_id`.
**Covers:** REQ-001 – REQ-008.
**Why here:** every subsequent route needs a principal, and identity-is-token-derived is a graded property, not a prerequisite to rush.

## Stage 3 — Shows and seats

Migrations for `events`, `shows`, `seats` with every constraint and index from [03-data-model.md](03-data-model.md). The effective-status expression in `db/sql.py`, defined once. `POST /shows` admin-only, show plus seats in one transaction, multi-row insert. `GET /shows/{id}` with the single-snapshot counts query. `GET /shows`.

**Exit:** show creation is atomic and rejects duplicate labels; counts satisfy the invariant; admin-only enforced; `ck_seats_hold_coherent` rejects an incoherent write.
**Covers:** REQ-010 – REQ-014.

## Stage 4 — The atomic claim

**The stage this service is judged by.** `user_show_quota`, `reservations`, `reservation_seats` with `uq_seat_active_claim`. `idempotency_keys`. `seat_repo.claim_one` and `claim_many` exactly as written in [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md). The two-transaction reserve flow. Quota lock, derived limit count. Full idempotency lifecycle. `POST /shows/{id}/reserve`.

**Exit:** the entire concurrency suite passes — hot-seat storm, multi-seat all-or-nothing under contention, per-user limit under parallel fire, concurrent duplicate keys. Negative controls fail against deliberately broken variants. Zero 5xx. `grill` has specifically attacked this stage.
**Covers:** REQ-020 – REQ-029, REQ-060.
**Note:** no other work proceeds in parallel with this stage. It gets undivided attention and the most adversarial review.

## Stage 5 — Lifecycle

`confirm` and `cancel`, owner-only via a `WHERE` clause, idempotent, guarded on current ownership. `hold_sweeper` with `SKIP LOCKED`. Reservation reads.

**Exit:** expiry frees seats both lazily and by sweep; a released seat is cleanly re-bookable; a cancel or confirm cannot resurrect or steal a seat that has moved on; the claim-racing-expiry test passes; non-owner access returns 404.
**Covers:** REQ-030 – REQ-036.

## Stage 6 — Observability

Metric catalogue from [10-observability.md](10-observability.md). Domain counters inline in services. Gauge refresher with the cardinality cap. `audit_log` migration, `AuditMiddleware`, bounded queue, batched `audit_writer`, shutdown flush. `RateLimitMiddleware` with per-class env-tunable ceilings.

**Exit:** `/metrics` valid and complete; counters reconcile with the database; no unbounded label; audit records every request without blocking; a saturated queue drops and counts rather than stalling; rate limiting does not throttle a legitimate stampede of distinct principals.
**Covers:** REQ-042, REQ-043, REQ-046, REQ-047.

## Stage 7 — Burst and hardening

`burst/burst.py`, `burst.sh`, `make burst`, all nine phases. Run against the live URL. Pool and timeout tuning driven by what the burst actually shows. Reconciliation polling during load.

**Exit:** burst passes against the deployed URL — exactly one winner per hot seat, zero 5xx, invariant held on every sample, metrics agree, spoof ignored, non-zero exit on any violation. Verified at full scale, not reduced.
**Covers:** REQ-013, REQ-021, REQ-048, and end-to-end verification of everything prior.
**Why last among the mandatory stages:** this is the stage that produces evidence. Everything before it is a claim.

## Stage 8 — Documentation

README per the contents list in [13-deployment.md](13-deployment.md). `WRITEUP.md`: the atomic mechanism and why it is race-free, multi-seat deadlock avoidance, idempotency storage and enforcement, holds and expiry, consistency versus availability under partition, what would page someone at 2am, what would come next. Final clean-clone verification on a fresh directory.

**Exit:** a reviewer with only the README can clone, run, burst, and read the live metrics without asking a question.

## Stage 9 — Monitoring view (optional)

Read-only page over `audit_log`: request rate, status distribution, declines by code, recent failures, each linking to a request id. Strictly off the request path, reading aggregates only.

**Entry condition:** Stages 0–8 fully closed. This is additive and must not be started while any correctness gate is open.

---

## Status

| Stage | State |
|---|---|
| Planning — `mds/` design set | **complete** |
| Team — agents and skills | **complete** |
| 0 Foundation | not started |
| 1 Database and first deploy | not started |
| 2 Auth and RBAC | not started |
| 3 Shows and seats | not started |
| 4 The atomic claim | not started |
| 5 Lifecycle | not started |
| 6 Observability | not started |
| 7 Burst and hardening | not started |
| 8 Documentation | not started |
| 9 Monitoring view | optional |

## Local environment

Provisioned 2026-10-03. Versions are pinned here because the container must match them.

| Component | Version | How |
|---|---|---|
| PostgreSQL | 16.15 | `brew install postgresql@16`, `brew services start postgresql@16` — keg-only, so `/opt/homebrew/opt/postgresql@16/bin` must be on `PATH` |
| Python | 3.13.16 | `brew install python@3.13`; venv at `.venv` |
| Docker | 29.8.2 CLI on Colima | `brew install colima docker docker-compose`, then `colima start --cpu 2 --memory 4 --disk 20` |
| Compose | 5.6.0 | Homebrew installs it standalone, so `docker compose` only works after `ln -sfn /opt/homebrew/opt/docker-compose/bin/docker-compose ~/.docker/cli-plugins/docker-compose` |

Colima runs the daemon in a Linux VM rather than Docker Desktop, so no admin privileges or GUI are involved — but the VM is 2 CPUs and ~3.8 GiB, which is the ceiling for anything run in a container locally. Full-scale load generation runs on the host against the deployed service, not inside this VM.

Databases: `seatres` (development) and `seatres_test` (tests), owned by role `seatres`. Local credentials are development-only and must never appear in a committed file; `DATABASE_URL` comes from `.env`, which is git-ignored.

Two numbers from this setup that the design depends on:

- **`max_connections` is 100** locally. The pool is sized against the server ceiling, not against request concurrency ([11-scalability.md](11-scalability.md)), and the test suite draws from the same 100. See LEARN-003.
- **The claim mechanism is verified against this server**, not merely specified. See LEARN-002 for the four probes and their results.

System Python 3.9 is too old and is not used. Homebrew Python 3.14 is present but 3.13 is the pinned version, matching the container base image.
