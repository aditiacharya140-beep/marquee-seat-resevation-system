# Testing and the burst script

The governing principle: a test that would still pass against a read-then-write implementation proves nothing about this service. Before trusting a concurrency test, break the implementation deliberately and confirm the test fails. A test never observed failing is not evidence.

**No mocked database anywhere.** Every correctness property here is a property of PostgreSQL's locking semantics. A mock would assert that the code calls a function, which is precisely the thing that does not matter.

## How the suite runs

`pytest`, 329 tests, about 16 seconds. Coverage of `app/` is 96% of lines and branches (`coverage run --branch --source=app -m pytest`).

- The application is driven in-process through `httpx.ASGITransport` — the real app factory, middleware chain and handlers — against `TEST_DATABASE_URL`, never the development database.
- The schema comes from the **real migration**, run by the same Alembic command the container entrypoint uses.
- `ASGITransport` does not run the lifespan, so a fixture opens and closes the pool per test.
- Every async test is wrapped in a hard timeout, so a deadlock fails instead of hanging the run (LEARN-005).
- Each test creates its own show and its own principals; nothing is truncated between tests.
- Rate limiting is off for the suite, because every test client shares one address; the rate-limit tests switch it on for themselves.

## What is tested, by file

### Unit — `tests/unit/`

| File | Covers |
|---|---|
| `test_config.py` | Required values absent refuse the boot; every relationship check rejects a violating environment; no secret is printed on a boot failure; `.env.example` and `Settings` declare the same set |
| `test_error_registry.py` | The registry equals the documented codes, in both directions, with matching statuses |
| `test_logging_redaction.py` | The log schema, and that sensitive keys never reach a line, including nested ones |
| `test_canonical_json.py` | Equal requests fingerprint equally; a different show, seat set or TTL does not |

### Integration — `tests/integration/`

| File | Covers |
|---|---|
| `test_ops.py`, `test_request_context.py` | `/healthz`, the access log and its exemptions, the startup line, request-id adoption and echo on every path including 422 and 500, the unhandled-exception counter |
| `test_readyz.py` | Ready from a real query; not ready on the very next call once the database is gone, with liveness unaffected; session guards survive a connection's release; the migration created the load-bearing constraints |
| `test_schema_constraints.py` | `ck_users_creds`, `ck_seats_hold_coherent`, `uq_seats_show_label` and `uq_seat_active_claim` fired directly; the quota table stores no tally |
| `test_errors.py` | The router's own 404 and 405 answer in the envelope |
| `test_auth.py` | Register, login with indistinguishable failures, guest, refresh (and that neither token type works as the other), guest upgrade keeping the same id, two concurrent upgrades having one winner, every token failure being the same 401, admin bootstrap being idempotent |
| `test_shows.py` | Creation, admin-only, each validation cause, per-seat price and section, the keyset-paginated list |
| `test_reserve.py` | Confirm-by-default and hold, the spoofed `user_id` ignored, all-or-nothing, a decline releasing its key, replay as 200, key reuse across seats and across shows, the per-user limit, each request-level rejection |
| `test_reserve_edges.py` | A key still in progress declining after the wait bound; stale takeover reserving exactly once; a taken-over owner unable to proceed; a lock timeout on reserve, confirm and cancel being a 409; a lapsed hold reading `expired`, freeing the limit and refusing a cancel; `SHOW_NOT_ON_SALE`; request-id stamping on rows; non-owner confirm and read being 404; unstorable text being a 422; a token for a missing user being a 401 |
| `test_lifecycle.py` | Cancel of a hold and of a confirmed booking, owner-only, re-booking by someone else, a repeat cancel never taking the seat back, confirm, a principal listing only its own reservations |
| `test_admin.py` | A reserve audited with who, what and outcome; the admin endpoints refusing non-admins; a full audit buffer dropping and counting while every request succeeds; the overview's aggregates; the log view, and that no secret is in it; the admin account following configuration |
| `test_frontend.py` | The pages and their files are served and spend no rate-limit allowance |
| `test_metrics.py` | Counters and the gauge agreeing with the API; no unbounded label |
| `test_rate_limit.py` | Guest creation capped per address; a forged `X-Forwarded-For` not choosing the bucket; distinct principals behind one address never throttled; exemptions; refill; route classes |

### Concurrency — `tests/concurrency/test_reserve_races.py`

One test per invariant. Every request runs on its own pooled connection, so the database arbitrates exactly as it would between separate clients.

| Test | Asserts |
|---|---|
| Hot seat | 60 principals, one seat: exactly one 201, 59 × 409 `SEAT_TAKEN`, no unhandled exception, one seat confirmed |
| Per-user limit | One principal, 12 parallel single-seat requests, limit 4: exactly 4 × 201 and 8 × `PER_USER_LIMIT` |
| One key fired concurrently | 20 identical requests: one 201, 19 × 200 replays, identical bodies, one reservation |
| Opposite-order multi-seat claims | 40 principals asking for the same four seats in opposite orders: one winner, no deadlock, no 5xx |
| Reconciliation during load | 80 principals on 20 seats while a sampler polls `GET /shows/{id}`: the invariant holds on every sample, confirmed equals the seats in 201 bodies, no seat sold twice |
| Lapsed hold | A one-second hold lapses; ten principals claim: one 201 and nine 409, the former holder's confirm is `RESERVATION_EXPIRED` |

**Negative control.** With the claim predicate replaced by `true`, four of these six fail; the other two test the limit and the key, which do not depend on that predicate. That was run by hand once. An automated module with injected broken variants is in [17-future-scope.md](17-future-scope.md).

### Adversarial review

One round, by a separate reviewing agent executing probes against PostgreSQL (LEARN-017). It could not produce a double-sell, a deadlock, a limit breach or two reservations for one key across several thousand randomized attempts, and found three inputs that returned 500 where a 4xx was owed. Each is fixed and has a test above.

## The burst script

`./burst.sh <BASE_URL>` wraps `burst/burst.py`. One command, any base URL. It needs `ADMIN_EMAIL` and `ADMIN_PASSWORD`, because it creates its own show — which is what makes every count it checks exact.

| Phase | What it does |
|---|---|
| Warm | Polls `/readyz` until ready, so a cold start is never measured as load |
| Setup | Logs in as admin, creates a fresh show, mints one guest per buyer, or with `--accounts` registers one account per buyer (ADR-037) — waiting as `Retry-After` instructs if that is throttled, and stopping if it would take longer than an access token lives |
| Stampede | Every buyer reserves one or two random seats, while a sampler polls the invariant |
| Hot seat | A barrier-released crowd on one seat. No connection is held while waiting on the barrier |
| Idempotent retries | One key fired twenty times at once |
| Limit probe | One principal fires more parallel reserves than the limit allows |
| Identity and release | A reserve carrying another user's id in the body belongs to the token's user; that other user's cancel is 404; the owner's cancel succeeds; the seat is re-booked by someone else; a repeat cancel leaves it with its new owner |
| Reconcile | The API's counts, the seats in 201 bodies, `/metrics` and `unhandled_exceptions_total` against each other |

It prints each check, the outcome distribution by status and code, and p50/p95/p99 latency timed from when a request leaves the script's own concurrency limiter. A response that is not the service's JSON is recorded with its real status and whether the service or the platform's proxy sent it.

**Exit is non-zero on any violation**: an invariant sample that does not sum, a hot seat without exactly one winner, a key that produced anything but one 201 and replays, a limit breach, a seat sold twice, any 5xx or dropped request, a metric disagreeing with the API, or `unhandled_exceptions_total` moving.

Flags: `--users`, `--seats`, `--hot`, `--concurrency`, `--timeout`, `--seed`.

## CI

`.github/workflows/ci.yml` runs lint and types, the suite against a Postgres service container, and a container job that builds the image, asserts its contents, asserts it refuses to boot without a secret, and waits for `/readyz`. **It has never run**: GitHub Actions is not enabled for the repository. Until it does, "passes" means "passes on the development machine".
