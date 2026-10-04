# Marquee

An assigned-seat reservation service for ticketed events — a cinema screening, a
concert. One seat, one buyer, under any amount of contention: N simultaneous requests
for the same seat produce exactly one `201` and N−1 clean `409`s. Never a double-sell,
never a `5xx`.

FastAPI · asyncpg · PostgreSQL 16 · Alembic · Prometheus · Docker.

| | |
|---|---|
| **Live URL** | <https://seat-reservation-vw5k.onrender.com> |
| API docs (interactive) | <https://seat-reservation-vw5k.onrender.com/docs> |
| Metrics | <https://seat-reservation-vw5k.onrender.com/metrics> |
| **Admin console** — audit trail, live logs, system health, create and delete shows | <https://seat-reservation-vw5k.onrender.com/admin> |
| **Admin sign-in** (fixed, published for reviewers) | `admin@example.com` / `seat-admin-2026` |
| Booking page | <https://seat-reservation-vw5k.onrender.com/> |
| Design decisions | [WRITEUP.md](WRITEUP.md) |

> **Before load-testing the live URL, read [Limitations](#limitations).** It is a free
> instance with a fraction of a CPU: about 19 bookings a second. The same container
> runs anywhere with `docker compose up`, which is where 20,000 buyers were tested.

![The seat map](mds/img/frontend.png)

## Testing it yourself, in five requests

Everything a load script needs. No sign-up: `POST /auth/guest` returns a user token
instantly, and it is the fast way to get thousands of users.

```bash
BASE=https://seat-reservation-vw5k.onrender.com     # or http://localhost:8080

# 1. Admin token (the admin creates shows)
ADMIN=$(curl -s $BASE/auth/login -H 'content-type: application/json' \
  -d '{"email":"admin@example.com","password":"seat-admin-2026"}' | jq -r .access_token)

# 2. Create a show — every seat starts "available"
SHOW=$(curl -s $BASE/shows -H "authorization: Bearer $ADMIN" -H 'content-type: application/json' \
  -d '{"name":"friday-night","seats":["A1","A2","A3"],"price_paise":25000}' | jq -r .id)

# 3. A user token — one request, no body
TOKEN=$(curl -s -X POST $BASE/auth/guest | jq -r .access_token)

# 4. Reserve — idempotency key in the header or in the body
curl -s $BASE/shows/$SHOW/reserve -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"seats":["A1","A2"],"idempotency_key":"order-1"}'
# 201 {"reservation_id":…,"show_id":…,"user_id":…,"seats":["A1","A2"],
#      "amount_paise":50000,"status":"confirmed",…}

# 5. State and metrics
curl -s $BASE/shows/$SHOW | jq .counts            # available + held + confirmed == total
curl -s $BASE/metrics | grep -E 'reservations_|seats_available'
```

Things worth knowing before you point a script at it:

- **Tokens last one hour** on the live service. After that a request answers
  `401 UNAUTHENTICATED`.
- **Use guest tokens for load.** `POST /auth/register` and `/auth/login` hash a
  password with Argon2, which is deliberately slow; a few dozen at once will saturate
  the free instance. `POST /auth/guest` does no hashing.
- **The booking *page* asks you to sign in before it books; the API does not.** That
  is a decision about the page only (a guest's ticket was lost when its tab closed).
  `POST /shows/{id}/reserve` accepts any valid token, guest or registered.
- **Rate limiting is switched off on the live service**, so a burst from one machine
  is never answered with `429`. `/readyz` reports `"rate_limit_enabled": false`.
- **A retry with the same idempotency key answers `200`**, with the original
  reservation and `Idempotent-Replay: true` — never a second `201`.

## How it meets the brief

| The brief asks for | What the service does | Proven by |
|---|---|---|
| `POST /shows` (admin) returns the show with an id and every seat available | Show and all seats created in one transaction; `id` and `show_id` both carry the id. Non-admin `403`, anonymous `401` | `tests/integration/test_shows.py` |
| `POST /shows/{id}/reserve`, identity from the token, idempotency key in header or body | As specified; `201` with `reservation_id`, `show_id`, `user_id`, `seats`, `amount_paise`, `status: "confirmed"` | `test_reserve.py` |
| **No double-sell**; one winner, losers get `409`, never `500` | One guarded `UPDATE` under a row lock decides it; a partial unique index is the backstop | `tests/concurrency/`, the burst, the adversarial review |
| **Per-user limit** (default 4), clean decline | Serialised per user by a row lock, counted from the seats themselves. `409 PER_USER_LIMIT` | `test_one_principal_never_exceeds_the_limit`, burst limit probe |
| **Idempotency**: same key once; same key, different seats → `409` | Key row with a unique constraint, completed in the same transaction as the reservation. `409 IDEMPOTENCY_KEY_REUSED` | `test_reserve.py`, `test_reserve_edges.py`, burst |
| **Partial requests**: define and hold under concurrency | **All-or-nothing.** If any seat is taken, nothing is claimed; the `409` names the conflicting seats | `test_a_partly_taken_request_claims_nothing`, opposite-order race test |
| **Release**: cancel *or* expiring hold; re-bookable; never resurrects | **Both.** Owner-only `POST /reservations/{id}/cancel` for a booking or a hold; and opt-in holds (`hold_ttl_seconds`) that expire on their own | `test_lifecycle.py`, lapsed-hold race test, burst release probe |
| `GET /shows/{id}` with per-seat status and counts; the invariant always holds | Counts are tallied from the same rows as the seat list, one snapshot | Sampled *during* every burst |
| Liveness, and readiness that fails closed | `/healthz` touches nothing; `/readyz` runs a real query and answers `503` when the database is gone | `test_readyz.py` |
| Metrics: confirmed, declined by reason, seats available; must reconcile | `reservations_confirmed_total`, `reservations_declined_total{reason}`, `seats_available{show_id}` computed at scrape time | `test_metrics.py`, burst reconciliation |
| Structured logs with a request id, and log access | Single-line JSON, `request_id` on every line; **readable in the admin console** | `test_request_context.py`, `test_admin.py` |
| One-command burst with a hot-seat storm, outcome distribution, reconciliation | `./burst.sh <BASE_URL>` or `make burst URL=…` | Output below |
| Containerised; clean checkout runs as deployed | `Dockerfile`, `docker-compose.yml`; the entrypoint runs migrations | Verified from a clean clone |
| Zero 5xx across a 20,000-reservation burst | Verified at that size locally. See [Limitations](#limitations) for the live instance | Evidence below |
| Identity is token-derived; only the owner may cancel | No request body has an identity field. A non-owner gets `404` | `test_reserve.py`, burst identity probe |
| Money is integer paise | `BIGINT` in the database, strict integers in the API; a float price is `422` | `test_shows.py` |

## Run it

```bash
docker compose up --build        # Postgres + the service; migrations run at start
curl localhost:8080/readyz       # {"status":"ready",…}
open http://localhost:8080/      # booking page; /admin for the console
```

Admin sign-in is the same: `admin@example.com` / `seat-admin-2026`.

Without Docker, against a local PostgreSQL 16:

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env             # then set DATABASE_URL and TEST_DATABASE_URL
make migrate run                 # http://localhost:8000
make lint types test             # ruff, mypy --strict, pytest (329 tests)
```

Every setting is documented in [.env.example](.env.example).

## Burst it

One command, any base URL. It needs the admin sign-in because it creates its own
show — which is what makes every number it checks exact.

```bash
# the live service (free instance: keep concurrency modest)
ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=seat-admin-2026 \
  ./burst.sh https://seat-reservation-vw5k.onrender.com --concurrency 50

# the same thing through make
ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=seat-admin-2026 \
  make burst URL=https://seat-reservation-vw5k.onrender.com

# the brief's scale, on your own machine
docker compose up --build -d
ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=seat-admin-2026 \
  ./burst.sh http://localhost:8080 --users 20000 --seats 4000 --hot 500 --concurrency 5000
```

Flags: `--users` buyers in the stampede (400) · `--seats` general seats (200) ·
`--hot` contenders for the one hot seat (150) · `--concurrency` requests in flight
(200) · `--accounts` register an account per buyer instead of a guest · `--timeout` ·
`--seed`.

What it does, in order: waits for `/readyz` (so a cold start is never measured as
load) → creates a fresh show → gets one user token per buyer → **stampede** on random
seats while sampling the invariant mid-flight → **hot-seat storm**, a
barrier-released crowd on one seat → **one idempotency key fired twenty times at
once** → **limit probe**, one user firing more parallel reserves than the limit →
**identity and release**: a spoofed `user_id` is ignored, a non-owner cannot cancel,
the owner can, and the seat is re-booked → **reconciles** the API's counts, the seats
in the `201` bodies and `/metrics` against each other.

It **exits non-zero on any violation**. Output from the live service:

```
target https://seat-reservation-vw5k.onrender.com
show 6084e317-…: 214 seats, 400 buyers, 150 on the hot seat

  [ok] reconciliation held in all 23 samples taken during the stampede
  [ok] hot seat: exactly one 201 (got 1)
  [ok] hot seat: 149 x 409 SEAT_TAKEN
  [ok] one key x20: one 201 (got 1)
  [ok] one key x20: the rest replay as 200 (got 19)
  [ok] one key: every response names the same reservation
  [ok] limit probe: exactly 4 of 10 parallel requests win (got 4 won, 6 PER_USER_LIMIT)
  [ok] spoofed user_id in the body is ignored: the booking belongs to the token's user
  [ok] another user cannot cancel it (got 404, expected 404)
  [ok] the owner can cancel it (got 200)
  [ok] the released seat is re-bookable (got 201 created)
  [ok] a repeat cancel does not take the seat back from its new owner
  [ok] available + held + confirmed == total ({'available': 43, 'held': 0, 'confirmed': 171, 'total': 214})
  [ok] no seat sold twice (171 seats sold)
  [ok] API confirmed count (171) == seats in 201 responses (171)
  [ok] zero 5xx and zero dropped requests (got 0)
  [ok] metrics seats_available (43) == API available (43)
  [ok] unhandled_exceptions_total did not move (delta 0)

581 reserve requests in 30.7s
latency ms: p50 2104  p95 6503  p99 7642
outcomes:
      19  200 idempotent_replay
     133  201 created
       6  409 PER_USER_LIMIT
     423  409 SEAT_TAKEN

PASSED: every invariant held
```

Each burst leaves its show behind. Delete one from the admin console's **Shows** tab
or with `DELETE /shows/{id}`. To remove all of them at once, straight from the
database (it asks first):

```bash
.venv/bin/python scripts/delete_burst_shows.py "<DATABASE_URL>"   # omit for the local ./.env one
```

`scripts/seed_demo.py <BASE_URL>` does the opposite: it fills a deployment with a
programme of six films, about a third of each sold, so the page has something to show.

## API

| Method | Path | Who | |
|---|---|---|---|
| `POST` | `/auth/guest` | public | a user token, no body, no sign-up |
| `POST` | `/auth/register` · `/auth/login` | public | a user token and a refresh token |
| `POST` | `/auth/refresh` | public | a refresh token in, a new access token out |
| `POST` | `/auth/upgrade` | guest | the same account gains an email and password; its bookings stay |
| `GET` | `/auth/me` | any user | |
| `POST` | `/shows` | admin | a show and all its seats; `seat_overrides` for per-seat price and section |
| `DELETE` | `/shows/{id}` | admin | removes the show, its seats and every reservation on it; no undo |
| `GET` | `/shows` | public | paginated list, newest first |
| `GET` | `/shows/{id}` | public | per-seat status and counts, one snapshot |
| `POST` | `/shows/{id}/reserve` | any user | **the atomic claim**; idempotency key required |
| `POST` | `/reservations/{id}/cancel` | owner | releases a booking or a live hold; the seats are re-bookable at once |
| `POST` | `/reservations/{id}/confirm` | owner | turns a hold into a booking |
| `GET` | `/reservations` · `/reservations/{id}` | owner | own reservations only |
| `GET` | `/healthz` · `/readyz` · `/metrics` | public | liveness · readiness with a real query · Prometheus |
| `GET` | `/admin/overview` · `/admin/audit` · `/admin/logs` · `/admin/shows` | admin | what the admin console reads |
| `GET` | `/` · `/admin` · `/static/*` | public | the booking page and the admin console |

## Semantics a client must code against

- **A reserve confirms immediately.** That is the brief's `"status": "confirmed"`.
  Pass `hold_ttl_seconds` to hold instead: the reservation is `held`, with
  `expires_at`, and must be confirmed before it lapses. A lapsed hold's seats are
  claimable at once, with no background job.
- **All-or-nothing.** Ask for `["A12","A13"]` with one taken and nothing is claimed.
  `409 SEAT_TAKEN`, with `details.conflicts` naming the seats that were taken.
- **Idempotency.** The key comes from the `Idempotency-Key` header or the body's
  `idempotency_key`; if both are sent and differ, `422`.
  Same key and same request → the original reservation, `200`, `Idempotent-Replay:
  true`. Same key and different seats, or a different show → `409
  IDEMPOTENCY_KEY_REUSED`. Two identical requests in flight at once → one reservation;
  the other waits briefly and replays it. A *declined* request releases its key, so
  retrying it is a real new attempt.
- **Per-user limit** is exact under concurrency: ten parallel requests against a
  limit of four end with four. `409 PER_USER_LIMIT`, with `details.limit` and
  `details.currently_held`. Cancelling frees the allowance.
- **Cancel.** The owner can cancel a confirmed booking or a live hold; the seats go
  back to available. Anyone else gets `404`. A repeat cancel is `200` and never takes
  a seat back from whoever booked it since. A hold that already lapsed answers
  `409 RESERVATION_EXPIRED`.
- **Identity is the token's user.** No request body has an identity field; a
  `user_id` in a payload is ignored, and the booking belongs to the token's user.
- **Ownership failures are `404`**, not `403`, so reservation ids cannot be enumerated.
- **Errors** share one envelope: `{"error": {"code", "message", "details",
  "request_id"}}` — including `404` for an unknown route and `422` for a bad body.
  Every response carries `X-Request-ID`.
- **Decline codes on reserve:** `SEAT_TAKEN`, `PER_USER_LIMIT`,
  `IDEMPOTENCY_KEY_REUSED`, `IDEMPOTENCY_IN_PROGRESS`, `SHOW_NOT_ON_SALE` (all `409`),
  `SHOW_NOT_FOUND`, `SEAT_NOT_FOUND` (`404`), `VALIDATION_ERROR` (`422`),
  `UNAUTHENTICATED` (`401`). A contended seat can also answer `409 SEAT_TAKEN` with
  `details.reason: "lock_timeout"` when the wait for it exceeds two seconds.
- **Money** is integer paise everywhere.

## Observability

**Metrics** — `GET /metrics`, Prometheus text.

| Series | Meaning |
|---|---|
| `reservations_confirmed_total` | reservations that reached confirmed |
| `reservations_declined_total{reason}` | reserve requests that created nothing: `seat_taken`, `per_user_limit`, `idempotent_replay`, `lock_timeout`, … |
| `seats_available{show_id}` | read from the database at scrape time, by the claim's own rule, so it cannot disagree with the API |
| `reservations_held_total`, `reservations_cancelled_total` | holds taken; reservations cancelled |
| `unhandled_exceptions_total` | **must stay at zero** — the direct measure of "no 5xx" |
| `audit_records_written_total`, `audit_records_dropped_total`, `audit_queue_depth` | the audit write path |

Counters are in memory and restart with the process; the audit trail and the API's
own counts are the durable record.

**Logs** — single-line JSON on stdout, `request_id` on every line, declines at
`info` so the error stream contains only faults, secrets redacted before a line is
written. Render's log stream is private, so the admin console's **Logs** tab is the
public log access.

**Audit trail** — one row per request: who, which show, which seats, status,
duration, outcome, request id. The request path appends to an in-memory buffer and
never waits; a full buffer drops the record and counts it, so audit can never slow a
booking.

**Admin console** — <https://seat-reservation-vw5k.onrender.com/admin>, sign in with
`admin@example.com` / `seat-admin-2026`.

![The admin console's overview after a burst](mds/img/admin.png)

| Tab | Shows |
|---|---|
| **Overview** | Requests, successes, declines and server errors over a chosen window; requests per minute; declines by reason; latency (p50/p95) per route; database connections in use; the audit buffer; every counter |
| **Shows** | Create a show; recent shows with seats available; delete a show and everything booked on it |
| **Audit trail** | One row per request, filterable by status, outcome, request id or show. Click a request id to see its log lines |
| **Logs** | The service's log lines, newest first, filterable by level, event and request id |

To watch a burst: open the console, start `./burst.sh`, and watch Overview refresh.

The log view is this process's most recent 2,000 lines, held in memory. It empties
when the service restarts, which on the free tier includes every wake from sleep.

## Limitations

Stated plainly, because several will show up in a load test.

**The live instance is a free tier and cannot be scaled from here.**

- A fraction of one CPU. Measured on it: about **19 bookings a second**; at 50
  requests in flight, half are answered within 2.1s and 95% within 6.5s.
- A burst of 20,000 concurrent reservations would take it roughly 17 minutes of
  work. Long before that, clients and the platform's own proxy will time requests
  out — and a timeout or a `502` from the platform is not something this service
  can prevent. **For that scale, run the container yourself** (above).
- It sleeps after ~15 minutes idle; the first request then takes up to a minute.
- The free database is 1 GB and expires after the platform's free period.

**What was verified at 20,000.** On a laptop, one process, pool of 20, 5,000 requests
in flight: 20,531 reserves in 99 seconds — 2,998 created, 17,508 `SEAT_TAKEN`, one
winner of 500 on the hot seat, 3,895 seats sold and none twice, the invariant held on
every sample, **zero 5xx and zero dropped requests**, 40,937 audit rows written and
none dropped. Latency at that depth was poor (half over 11s): one Python process.

**How the live service is configured**, where it differs from `.env.example`:

| Setting | Live | Why |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `false` | The brief asks for one `201` and `409` for everyone else; a limiter would turn a load test from one machine into `429`s. The limiter is built and tested, and switched off here |
| `ACCESS_TOKEN_TTL_SECONDS` | `3600` | A token minted at the start of a long test still works at the end. After an hour: `401` |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | published above | So a reviewer can create shows and read logs without asking |
| `RATE_LIMIT_TRUSTED_PROXY_HOPS` | `3` | Render puts three proxies in front; found by test. Only matters when limiting is on |

`docker-compose.yml` and `render.yaml` carry the same values, so a clean checkout
runs the way the live service does.

**Known weaknesses**

- **The admin sign-in is public.** Anyone who reads this can create shows, **delete
  shows along with every booking on them**, and read the audit trail and logs. They
  cannot read a password or a token. If a show you were testing against disappears,
  this is why; create another.
- **The per-user seat limit is per account, and accounts are free.** Guests cost
  nothing to create and there is no payment step, so one person can make several
  accounts and book more than four seats. With rate limiting on, guest creation is
  held to one a second per address; on the live demo it is off. Closing it needs a
  payment or identity step the brief does not include.
- **Registering and signing in are slow under load**, by design of password hashing.
- **A guest's session ends after an hour** and cannot be refreshed.
- **One process.** Counters, rate-limit buckets and the log view are per process and
  reset on restart.
- **A crash is logged twice**, the second time without its request id.
- **Tests have only run on the development machine.** GitHub Actions is not enabled.
- **Nothing purges old rows**: the audit trail and idempotency keys grow.

Everything designed and not built is in
[mds/17-future-scope.md](mds/17-future-scope.md).

## Layout

```
app/api/routes      HTTP only: parse, delegate, serialize
app/services        orchestration and transaction boundaries
app/repositories    all SQL — seat_repo.py is the claim
app/db              pool, session guards, migrations, the effective-status rules
app/middleware      request id, access log, rate limit, audit
app/core            config, errors, logging, security, metrics
app/static          the booking page and the admin console, served as they are
tests/concurrency   one race test per invariant, against real Postgres
burst/              the load script
scripts/            seed a programme; delete burst shows
mds/                the design, kept in step with the code — start at mds/00-overview.md
                    mds/19-brief-compliance.md maps the brief to the code
                    mds/17-future-scope.md is everything not built
```

## Deploy

[render.yaml](render.yaml) declares the web service and its PostgreSQL 16 database.
The container entrypoint runs the database migrations before it starts the server,
and a failed migration fails the start.

On any other host: build the `Dockerfile`, point it at a PostgreSQL 16 database with
`DATABASE_URL`, and set `JWT_SECRET` (32+ characters), `ADMIN_EMAIL`,
`ADMIN_PASSWORD` (12+ characters), `ALLOWED_EVENT_KINDS`, `DEFAULT_EVENT_KIND` and
`DEFAULT_CURRENCY`. `docker-compose.yml` is a complete working example.
