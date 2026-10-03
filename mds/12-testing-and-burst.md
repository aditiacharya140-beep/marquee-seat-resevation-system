# Testing and the burst script

The governing principle: a test that would still pass against a read-then-write implementation proves nothing about this service. Before trusting any concurrency test, break the implementation deliberately — remove the guard from the claim's `WHERE` clause — and confirm the test fails. A test never observed failing is not evidence.

---

## Layers

| Layer | Target | Database | Speed |
|---|---|---|---|
| Unit | `domain`, `helpers`, `utils` | none | milliseconds |
| Integration | routes through to the database | real Postgres | seconds |
| Concurrency | the invariants, under genuine parallelism | real Postgres | tens of seconds |
| Operational | health, readiness, metrics, cold start | real Postgres, container | seconds |
| Burst | the deployed service over HTTP | live | minutes |

**No mocked database anywhere.** Every correctness property in this service is a property of PostgreSQL's locking semantics. A mock would assert that the code calls a function, which is precisely the thing that does not matter. Integration and concurrency tests run against a real Postgres, created and migrated per session, with each test isolated by its own show.

---

## Unit

| Area | What is proven |
|---|---|
| Canonical JSON | Key reordering, whitespace, and seat-label ordering produce one fingerprint; a changed seat does not |
| Money | Integer paise throughout; totals exact at large quantities; a static check that no float touches a money field |
| Seat labels | Validation, normalization, duplicate detection, length bounds |
| Policies | Limit arithmetic, TTL clamping, price resolution with per-seat override |
| Config | Required values absent fails startup loudly; guest token lifetime exceeds hold TTL |
| Error registry | Every code maps to exactly one status; no code is defined twice; the registry matches the API contract table |

---

## Integration

Per-endpoint contract: status codes, response shape, error codes and envelope, authorization. Notably:

- `POST /shows` creates show and all seats in one transaction; a validation failure creates nothing — asserted by counting rows, not by reading the response.
- `GET /shows/{id}` reports a lapsed-but-unswept hold as `available`, matching what a claim would see.
- Unknown body fields are rejected 422, including `user_id`.
- Every error response carries the envelope and a request id matching the `X-Request-ID` header.
- An inbound `X-Request-ID` is adopted when a valid UUID and replaced when not, and the value reaches the `request_id` column of the rows the request wrote.

### Authorization probes

These are correctness requirements, not a security afterthought, and each is a distinct test:

- Reserve with `{"seats":["A1"],"user_id":"<other>"}` — the reservation belongs to the token's subject.
- Confirm and cancel another principal's reservation — 404, not 403, so ids cannot be enumerated.
- A `user` and a `guest` token on every admin route — 403, and no state change.
- A refresh token used as a bearer credential on a business route — 401.
- An expired token, a tampered signature, a wrong issuer, `alg: none` — each 401.
- A guest reserves, confirms, and cancels successfully; the same guest cannot reach account routes.
- A guest upgrades and its existing reservations remain attached to the same `user_id`.

---

## Concurrency

Run with real parallel clients against a real database. Barrier-synchronized so requests actually overlap — a loop of sequential awaits tests nothing.

### Hot-seat storm — REQ-021

N clients, one seat, released simultaneously.

```
assert status.count(201) == 1
assert status.count(409) == N - 1
assert all 409 bodies have code == "SEAT_TAKEN"
assert no 5xx
assert db: exactly one seats row for that label with held_by set
assert db: exactly one reservation_seats row with released_at IS NULL
```

"Zero winners" is as much a failure as "two winners". Both are asserted.

### Multi-seat all-or-nothing under contention — REQ-022

Two clients requesting overlapping sets, e.g. `[A12,A13]` and `[A13,A14]`, simultaneously. Exactly one succeeds with both its seats; the loser gets 409 listing `A13` **and holds nothing** — the critical assertion is that the loser's `A12` or `A14` is still `available`, because a partial claim left behind is the defect this test exists to find.

### Per-user limit — REQ-023

One principal, ten parallel single-seat reserves, limit 4. Exactly four 201s, six 409 `PER_USER_LIMIT`, and the database shows exactly four seats held by that principal. Repeated across limit and parallelism values from config.

### Concurrent idempotency — REQ-026

Two to ten clients, identical key, identical body, fired together. Exactly one reservation exists. Every response is either the same 201 body or a 409 `IDEMPOTENCY_IN_PROGRESS`; no two different reservation ids are ever returned for one key.

Separately: same key with a mutated body → 409 `IDEMPOTENCY_KEY_REUSED`, and no seat moved. Same key after a decline → the decline replays with its original status.

### Claim racing expiry — REQ-033, REQ-034

A hold about to lapse, with a competing claim and the sweeper running. Assertions: the seat ends owned by exactly one principal; the original holder's `confirm` either succeeds (it won) or returns 409 (it lost) but never takes the seat from a new owner; a cancel of a reservation whose seat has moved on affects zero rows.

### Reconciliation during load — REQ-013

A poller calls `GET /shows/{id}` continuously while the burst runs, asserting `available + held + confirmed == total_seats` on **every** sample. Sampling only after the burst is a materially weaker claim than the brief makes, and this test is the one that catches a drifted effective-status expression.

### Cross-check

After every concurrency test: API state, database rows, and Prometheus counters must agree. A counter disagreeing with the database is a reportable defect even when the API looks correct.

### Negative controls

For each of the three mechanisms — guarded claim, quota lock, idempotency key — a test runs against a deliberately broken variant and **must** fail. These are kept as explicit regression tests of the test suite itself, because a concurrency suite that cannot detect the bug it was written for is worse than none.

---

## Operational

- `/healthz` 200 with the database stopped; `/readyz` 503 naming `database`, never from a cache.
- `/metrics` parses as valid Prometheus text; every catalogued metric is present; no label carries a user id, seat label, or concrete path.
- Cold start: container from scratch reaches `/readyz` ready within the configured budget.
- Restart mid-burst: no double-sell, no stuck idempotency key, invariant intact afterwards.
- Graceful shutdown flushes the audit queue.
- A clean clone builds and runs via the documented command, in CI, with no manual step — this is the check for REQ-050, and the most common way a submission fails.

---

## The burst script

`burst/burst.py`, wrapped by `./burst.sh <BASE_URL>` and `make burst`. One command, any base URL, local or live.

### Phases

| Phase | Purpose |
|---|---|
| 0 — Warm | Poll `/readyz` until ready. Free tiers spin down; measuring a cold start as if it were load produces a meaningless result. |
| 1 — Provision | Authenticate as admin, create a fresh show with a configured seat count, mint the configured number of principals (guests by default — no credential setup, realistic for an on-sale). |
| 2 — Baseline | Capture `/metrics` and `GET /shows/{id}` before load. |
| 3 — Stampede | All principals reserve random seats concurrently, at the configured concurrency. |
| 4 — Hot-seat storm | All principals target one seat simultaneously, barrier-synchronized. Repeated for a configured number of hot seats. |
| 5 — Idempotent retries | A configured share of principals replay their exact request with the same key; a smaller share replay the same key with a mutated body. |
| 6 — Limit probe | One principal fires more parallel reserves than the limit permits. |
| 7 — Spoof probe | A reserve carrying another principal's id in the body; assert the reservation belongs to the token's subject. |
| 8 — Lifecycle | A share of winners confirm, a share cancel; cancelled seats are re-reserved to prove they are cleanly re-bookable. |
| 9 — Reconcile | Re-read `GET /shows/{id}` and `/metrics`; check the invariant and metric agreement. |

A reconciliation poller runs throughout phases 3 through 8, sampling the invariant continuously.

### Output

```
BURST  base_url=https://…  show_id=…  seats=500  principals=2000  concurrency=500

phase            requests   201     200     409     422   429   5xx    p50     p99
stampede             2000   487       0    1513       0     0     0    14ms    91ms
hot_seat_storm       2500     5       0    2495       0     0     0    11ms   140ms
idempotent_retry      400     0      380      20       0     0     0     6ms    22ms
limit_probe            10     4       0       6       0     0     0     9ms    31ms
lifecycle             300   180     120       0       0     0     0     8ms    27ms

declines by reason
  seat_taken                4008
  per_user_limit               6
  idempotent_replay          380
  idempotency_key_reused      20

hot seats                 winners
  A12                           1    ✓
  B07                           1    ✓
  C15                           1    ✓

invariant samples          412 / 412 held                       ✓
final counts               available 13  held 94  confirmed 393  total 500   ✓
metrics agree with API     yes                                   ✓
spoofed identity ignored   yes                                   ✓
5xx total                  0                                     ✓

VERDICT: PASS
```

### Contract

- Exit non-zero on **any** invariant violation, any 5xx, any hot seat with a winner count other than one, any metric disagreement, or a successful identity spoof. The script is a gate, not a report.
- Every parameter configurable by flag or environment variable — base URL, seat count, principal count, concurrency, hot-seat count, retry share, limit-probe size. Nothing hardcoded.
- Prints the actual outcome distribution. A script that only prints "PASS" is not evidence.
- Separates 429 from 409 in its own column, so a throttling artifact is never mistaken for a seat decline.
- Idempotent against a live service: creates its own show, touches nothing pre-existing.
- Uses a bounded connection pool with keep-alive. The goal is to load the service, not to exhaust local file descriptors and then report the client's failure as the server's.

---

## CI

On every push: lint, type check, unit, integration against a Postgres service container, concurrency suite at reduced scale, container build, and a smoke run of the built image from a clean checkout proving `/readyz` becomes ready. The full-scale burst runs against the deployed URL manually and before submission, because it is slow and needs the live target.
