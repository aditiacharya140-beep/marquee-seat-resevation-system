# Stage plan

Build order was chosen by **risk**, not by layer. The two things that sink this service are an unproven atomic claim and a deploy that does not come up, so both were to be proven on real infrastructure before breadth was added.

That is not quite what happened, and the difference is worth recording. Design and Stage 0 took most of the time available; everything from the database to the live burst was then built in one direct pass on the last day (ADR-032), and features cut to make that possible were restored afterwards (ADR-033, ADR-034). The stages below are the plan; the status table is what exists.

## Gate

A stage closes only when all five hold:

1. Acceptance criteria pass, each traced to a `REQ-*` id.
2. Tests exist that fail if the stage regresses.
3. `grill` has reviewed; every finding is fixed, refuted in writing, or logged as `RISK-*`.
4. Work is committed incrementally.
5. The deployment is still healthy.

**By that gate, only Stage 4's claim path has closed in full**: it is the one part with tests, an adversarial review, and a passing live burst. Every other stage is built and tested but unreviewed. That is the honest reading of the table below.

## Status

| Stage | Built | Tested | Reviewed | What is missing |
|---|---|---|---|---|
| 0 Foundation | yes | yes | yes | A crash is logged twice, once without its request id |
| 1 Database and first deploy | yes | yes | no | Cold-start time never measured |
| 2 Auth and RBAC | yes | yes | no | Refresh is stateless, so nothing can be revoked |
| 3 Shows and seats | yes | yes | no | No way to take a show off sale; sale windows unused |
| 4 The atomic claim | yes | yes | **yes** | Negative controls are not automated |
| 5 Lifecycle | yes | yes | partly, with stage 4 | No permanent test for a cancel racing a claim at the expiry boundary |
| 6 Observability | partly | yes | no | Rate limiting built; audit not built; only part of the metric catalogue exposed |
| 7 Burst and hardening | partly | — | no | Passed live at 200 buyers, never at full scale; no spoof or lifecycle phase |
| 8 Documentation | yes | — | — | No clean-clone verification; CI has never run |
| 9 Monitoring view | no | — | — | Depends on audit |

Everything in the right-hand column is described in [17-future-scope.md](17-future-scope.md).

## The stages

### Stage 0 — Foundation
Typed settings with startup validation, JSON logging with redaction, the error hierarchy and code registry, request correlation, the access log, `/healthz`, the container, compose, CI.
**Covers:** REQ-040, REQ-044, REQ-045, REQ-050.

### Stage 1 — Database and first deploy
One Alembic revision carrying the whole schema. The pool, with session guards as startup parameters. `/readyz` with a real query, failing closed. `render.yaml`. Deployed to Render.
**Covers:** REQ-041, REQ-049.

### Stage 2 — Auth and RBAC
Argon2 in a bounded executor. JWT with `typ` checks. Register, login, guest, upgrade, refresh, me. `get_current_user` and `require_admin`. Idempotent admin bootstrap.
**Covers:** REQ-001 – REQ-008.

### Stage 3 — Shows and seats
The effective-status expressions, defined once. `POST /shows` with all seats in one statement and per-seat overrides. `GET /shows/{id}` with counts from the seat rows' own snapshot. `GET /shows`, keyset-paginated.
**Covers:** REQ-010 – REQ-014.

### Stage 4 — The atomic claim
**The stage this service is judged by.** The ordered `FOR UPDATE` claim, the quota lock and derived count, superseded-row closure, the two-transaction reserve with the key row as an ownership token, `POST /shows/{id}/reserve`.
**Covers:** REQ-020 – REQ-029, REQ-060.

### Stage 5 — Lifecycle
`confirm` and `cancel` as guarded updates, owner-only by `WHERE` clause. Reservation reads with effective status. No sweeper (ADR-017).
**Covers:** REQ-030 – REQ-036.

### Stage 6 — Observability
Domain counters inline in services; the availability gauge at scrape time; `RateLimitMiddleware`. Audit is future scope.
**Covers:** REQ-042 (partly), REQ-043, REQ-047. REQ-046 is not met.

### Stage 7 — Burst and hardening
`burst/burst.py` and `burst.sh`, run against the live URL.
**Covers:** REQ-013, REQ-021, REQ-048.

### Stage 8 — Documentation
`README.md`, `WRITEUP.md`, and this set brought into line with the code (ADR-035).

### Stage 9 — Monitoring view
Not started; optional; depends on audit.

## What to do next, in order

1. Enable GitHub Actions and get the workflow green. Until then nothing has been verified off one machine.
2. Review the unreviewed stages — auth, shows, the rate limiter — with the same adversarial pass the claim path had.
3. Decide how a principal earns the right to reserve (item 1 of future scope). It is the only known weakness with a product consequence.
4. Run the burst at full scale against the live URL.
5. Audit, then the missing metrics.

## Local environment

| Component | Version | How |
|---|---|---|
| PostgreSQL | 16.15 | `brew install postgresql@16`, `brew services start postgresql@16` |
| Python | 3.13 | `brew install python@3.13`; venv at `.venv` |
| Docker | CLI on Colima | `brew install colima docker docker-compose`, then `colima start` |

Databases `seatres` (development) and `seatres_test` (tests), owned by role `seatres`. `DATABASE_URL` comes from `.env`, which is git-ignored. `max_connections` is 100 locally and the pool is sized against it (LEARN-003).
