# Seat Reservation Service

Assigned-seat booking for ticketed events. One seat, one buyer, under any amount of
contention: N simultaneous requests for the same seat produce exactly one `201` and
N−1 clean `409`s — never a double-sell, never a `5xx`.

**Live:** <https://seat-reservation-vw5k.onrender.com> ([/docs](https://seat-reservation-vw5k.onrender.com/docs) · [/readyz](https://seat-reservation-vw5k.onrender.com/readyz) · [/metrics](https://seat-reservation-vw5k.onrender.com/metrics)) · **How it works and why it is race-free:** [WRITEUP.md](WRITEUP.md)

FastAPI · asyncpg · PostgreSQL 16 · Alembic · Prometheus · Docker.

## Run it

```bash
docker compose up --build        # Postgres + API; migrations run in the entrypoint
curl localhost:8080/readyz       # {"status":"ready",...}
```

Without Docker, against a local PostgreSQL 16:

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env             # then set DATABASE_URL, JWT_SECRET, ADMIN_*
make migrate run                 # http://localhost:8000
make lint types test             # ruff, mypy --strict, pytest
```

## Burst it

```bash
ADMIN_EMAIL=... ADMIN_PASSWORD=... ./burst.sh <BASE_URL>
./burst.sh http://localhost:8080 --users 3000 --seats 1000 --hot 500 --concurrency 500
```

The burst creates its own show, so every number it checks is exact. It stampedes random
seats while sampling reconciliation mid-flight, releases a barrier-synchronised crowd
onto a single hot seat, fires one idempotency key twenty times at once, probes the
per-user limit with parallel requests, then reconciles the API, the `201` bodies and
`/metrics` against each other. It prints the outcome distribution by reason and
**exits non-zero on any invariant violation**.

## Quickstart

```bash
BASE=http://localhost:8080

# Admin (bootstrapped from ADMIN_EMAIL / ADMIN_PASSWORD) creates a show
ADMIN=$(curl -s $BASE/auth/login -H 'content-type: application/json' \
  -d '{"email":"admin@example.com","password":"dev-only-change-me"}' | jq -r .access_token)
SHOW=$(curl -s $BASE/shows -H "authorization: Bearer $ADMIN" -H 'content-type: application/json' \
  -d '{"name":"friday-night","seats":["A1","A2","A3"],"price_paise":25000}' | jq -r .show_id)

# A guest reserves — no sign-up needed
TOKEN=$(curl -s -X POST $BASE/auth/guest | jq -r .access_token)
curl -s $BASE/shows/$SHOW/reserve -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -H 'Idempotency-Key: order-1' \
  -d '{"seats":["A1","A2"]}'                      # 201, status "confirmed"

curl -s $BASE/shows/$SHOW | jq .counts            # available + held + confirmed == total
curl -s $BASE/metrics | grep -E 'reservations_|seats_available'
```

## API

| Method | Path | Who | |
|---|---|---|---|
| `POST` | `/auth/register` · `/auth/login` · `/auth/guest` | public | returns a bearer token |
| `POST` | `/auth/refresh` | public | a refresh token in, a new access token out |
| `POST` | `/auth/upgrade` | guest | same account gains an email and password; its bookings stay |
| `GET` | `/auth/me` | any principal | |
| `POST` | `/shows` | admin | show and all its seats, one transaction; `seat_overrides` for per-seat price |
| `GET` | `/shows` | public | paginated catalogue, newest first |
| `GET` | `/shows/{id}` | public | seat map and counts from one snapshot |
| `POST` | `/shows/{id}/reserve` | any principal | **the atomic claim**; `Idempotency-Key` required |
| `POST` | `/reservations/{id}/confirm` · `/cancel` | owner | for a hold |
| `GET` | `/reservations` · `/reservations/{id}` | owner | own reservations only, paginated |
| `GET` | `/healthz` · `/readyz` · `/metrics` | public | liveness · real DB query · Prometheus |

Interactive docs at `/docs`.

## Semantics worth knowing

- **A reserve confirms immediately.** Pass `hold_ttl_seconds` to hold instead; a hold
  must be confirmed before it lapses, and a lapsed hold's seats are claimable at once.
- **All-or-nothing.** If any requested seat is taken, nothing is claimed; the `409`
  lists the conflicting labels.
- **Idempotency.** Same key + same request → the original reservation, replayed as
  `200` with `Idempotent-Replay: true` (never a second `201`). Same key + different
  request, or a different show → `409 IDEMPOTENCY_KEY_REUSED`. A *declined* request
  releases its key, so retrying it is a real new attempt.
- **Per-user limit** is exact under concurrency: ten parallel requests against a limit
  of four end with four.
- **Identity is the token's subject.** No request body has an identity field; a
  `user_id` in a payload is ignored.
- **Ownership failures are `404`**, not `403`, so reservation ids cannot be enumerated.
- **Sessions.** An access token lasts 15 minutes; `/auth/refresh` exchanges the
  refresh token from register or login for a new one. Guests get no refresh token.
- **Lists** are keyset-paginated: pass the `next_cursor` from one page as `cursor` for
  the next; `null` means there are no more.
- **Errors** share one envelope: `{"error": {"code", "message", "details", "request_id"}}`.
  Every response carries `X-Request-ID`, and every row written is stamped with it.

## Layout

```
app/api/routes      HTTP only: parse, delegate, serialize
app/services        orchestration and transaction boundaries
app/repositories    all SQL — seat_repo.py is the claim
app/db              pool, session guards, migrations, the effective-status expressions
app/core            config, errors, logging, security, metrics
tests/concurrency   one race test per invariant, against real Postgres
burst/              the load script
mds/                the design: 16 documents, 31 ADRs — start at mds/00-overview.md
```

## Deploy

[render.yaml](render.yaml) declares the web service and its PostgreSQL 16 database. Set
`ADMIN_EMAIL` and `ADMIN_PASSWORD` in the dashboard; everything else is declared. The
free tier sleeps after ~15 minutes idle — the burst script waits on `/readyz` before
it measures anything.
