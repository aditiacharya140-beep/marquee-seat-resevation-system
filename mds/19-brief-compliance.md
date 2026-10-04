# The brief, line by line

What the problem statement asks for, what the service does about each line, and where the proof is. Written against the code as deployed; anything the brief asks for that is only partly met says so.

## Functional requirements

| # | The brief | The service | Proof |
|---|---|---|---|
| 1 | `POST /shows` (admin) with `name`, `seats[]`, `price_paise`; returns the show with an id and every seat `available` | As asked. The response carries the id as both `id` and `show_id`. Show and seats are created in one transaction. Optional: `per_user_limit`, `hold_ttl_seconds`, `event_kind`, `currency`, `seat_overrides`. Up to 50,000 seats | `tests/integration/test_shows.py` |
| 2 | `POST /shows/{id}/reserve`, authenticated; identity from the token; idempotency key in header or body | As asked. `201` with `reservation_id`, `show_id`, `user_id`, `seats`, `amount_paise`, `status: "confirmed"`, plus `currency`, `confirmed_at`, `created_at` | `test_reserve.py` |
| 2a | No double-sell; one winner; losers `409`, never `500` | One guarded `UPDATE` over an ordered `FOR UPDATE`; the rows returned are the decision. `uq_seat_active_claim` is the backstop. Losers get `409 SEAT_TAKEN` | `tests/concurrency/test_reserve_races.py`, the burst |
| 2b | Per-user limit, default 4; over-limit is a clean decline | A quota row lock serialises one user's requests; the count is derived from the seats. `409 PER_USER_LIMIT` | race test, burst limit probe |
| 2c | Same key reserves once; a retry returns the original; same key, different seats → `409` | Replay is `200` with the stored body and `Idempotent-Replay: true`. Different seats, or a different show, is `409 IDEMPOTENCY_KEY_REUSED` | `test_reserve.py`, `test_reserve_edges.py`, burst |
| 2d | Partial requests: define, document, hold under concurrency | **All-or-nothing**, by transaction rollback. Documented in the README | `test_a_partly_taken_request_claims_nothing`, opposite-order race |
| 3 | Release: explicit cancel (owner only) **or** an expiring hold; re-bookable; never resurrects | **Both.** `POST /reservations/{id}/cancel` for a confirmed booking or a live hold, owner only (`404` otherwise). Opt-in holds via `hold_ttl_seconds`, expiring lazily inside the claim's own predicate. Releases are guarded on the reservation's own id | `test_lifecycle.py`, lapsed-hold race, burst release probe |
| 4 | `GET /shows/{id}`: per-seat status and counts; the invariant holds at all times | Seat rows with effective status and `counts` tallied from those same rows — one statement, one snapshot | sampled during every burst |
| 5 | Health and metrics | Below | |

## The correctness bar

| # | The brief | Status |
|---|---|---|
| 1 | No seat confirmed to two users; one `201` per hot seat, everyone else `409` | **Met.** Live: 1 of 150. Local: 1 of 500, and 1 of 2,500 in the adversarial review |
| 2 | Zero 5xx across the burst | **Met at 20,000 locally** (20,531 reserves, zero 5xx, zero dropped). **Met live at the size the free instance can serve** (581 reserves). See the caveat below |
| 3 | The invariant holds to the unit, during and after | **Met.** Sampled mid-burst every time |
| 4 | Idempotent retries move nothing extra; different seats on the same key → `409` | **Met** |
| 5 | Per-user limit holds under concurrency | **Met.** Ten parallel on a limit of four end with four; 400 parallel in the review ended with four |
| 6 | Identity is token-derived; a spoofed body field cannot act as another user; only the owner may cancel | **Met.** No request model declares an identity field |

**The caveat on 2.** The live instance is a free tier with a fraction of a CPU, serving about 19 bookings a second. A 20,000-request burst against it will be timed out by clients and by the platform's proxy long before the service has answered, and a `502` or timeout from the platform is outside the service's control. The service itself does not answer a domain outcome with a 5xx: a request beyond the pool waits up to 60 seconds for a connection rather than being refused. The same image, run with `docker compose up`, is where the brief's scale was verified.

## Deploy and observe

| The brief | The service |
|---|---|
| Public URL, survives a cold start | `https://seat-reservation-vw5k.onrender.com`. The entrypoint migrates, then starts; `/readyz` gates traffic. The burst waits for `/readyz` before measuring |
| Containerised; a clean checkout runs as deployed | `Dockerfile`, `docker-compose.yml`, `render.yaml`. Compose and Render carry the same demo configuration. Verified from a clean clone |
| Liveness, and readiness that checks the database and fails closed | `/healthz`; `/readyz` runs `SELECT 1`, answers `503` with the dependency named when it cannot, and is never cached |
| Metrics: confirmed, declined by reason, seats available; reconcile with the API | `reservations_confirmed_total`, `reservations_declined_total{reason}` (`seat_taken`, `per_user_limit`, `idempotent_replay`, …), `seats_available{show_id}` read at scrape time by the claim's own rule. The burst checks the gauge against the API |
| Structured logs with a request id; public log access, or a recording | JSON lines with `request_id`. Render's log stream is private, so the **admin console's Logs tab** is the public access, with a published sign-in. Beyond the brief: an audit trail of every request |
| One-command burst, with a hot-seat storm, the outcome distribution and reconciliation | `./burst.sh <BASE_URL>` and `make burst URL=…`. Exits non-zero on any violation |

## Deliverables

| The brief | Status |
|---|---|
| Public Git repo with incremental history | History is incremental. **The repository must be made public before submission** — it was private when this was written |
| Live URL | Above |
| Burst script and how to run it, in the README | Yes |
| Metrics and logs access | `/metrics`; the admin console at `/admin` |
| `WRITEUP.md` covering the atomic decision, deadlock, idempotency, holds and expiry, partition behaviour, 2am pages, AI usage, what next | Yes, all eight |

## Ground rules

| The brief | Status |
|---|---|
| Money is integer paise | `BIGINT` columns, strict integers in the API; a float price is `422` |
| AI usage disclosed honestly | `WRITEUP.md`, and `16-decision-highlights.md` for the full record |
| A clean checkout builds and runs | Verified with `docker compose up --build` from a clean clone |

## Where the service goes beyond the brief

Guest accounts and upgrade; refresh tokens; per-seat pricing; list endpoints; confirm for holds; rate limiting (built, switched off on the demo); the audit trail; the admin console; a booking page; show deletion. None is required, and none is on the claim path.

## Where it differs from what a reader might assume

- **A retry answers `200`, not `201`.** The brief says a retry "returns the original reservation"; it does, with `Idempotent-Replay: true`. It is `200` so that counting `201`s counts claims.
- **A declined request does not burn its key.** Retrying a `409` with the same key is a fresh attempt.
- **A non-owner's cancel is `404`, not `403`**, so reservation ids cannot be probed.
- **The booking page requires an account; the API does not.** `POST /auth/guest` returns a usable token with no sign-up, and that is what a load test should use.
- **Rate limiting is off on the demo.** `/readyz` says so.
