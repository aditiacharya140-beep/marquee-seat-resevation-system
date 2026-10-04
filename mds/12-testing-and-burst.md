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
| Config | Required values absent fails startup loudly. Each of the three relationship checks in `13-deployment.md` rejects a violating environment: `GUEST_TOKEN_TTL_SECONDS > MAX_HOLD_TTL_SECONDS`, `DB_ACQUIRE_TIMEOUT_SECONDS * 1000 > DB_STATEMENT_TIMEOUT_MS`, and `DB_STATEMENT_TIMEOUT_MS >= DB_LOCK_TIMEOUT_MS + DB_TIMEOUT_MARGIN_MS`. `DEFAULT_EVENT_KIND` outside `ALLOWED_EVENT_KINDS` refuses to boot |
| Error registry | Every code maps to exactly one status; no code is defined twice; the registry's code set **equals** the `06-apis.md` contract codes union the operational-codes table in `08-error-logging.md`. Equality against the contract codes alone is unsatisfiable — the five operational codes appear in no endpoint table — and a subset relation would let a stray code in unnoticed |
| Constants | `EventKind` does not exist as an enum; the permitted set comes from configuration, and a grep for a vertical name in `app/` finds nothing (ADR-025) |
| Request models | No model anywhere declares an identity field, and each model's `extra` setting matches its route's audience: `forbid` for admin, `ignore` elsewhere (ADR-028) |

---

## Integration

Per-endpoint contract: status codes, response shape, error codes and envelope, authorization. Notably:

- `POST /shows` creates show and all seats in one transaction; a validation failure creates nothing — asserted by counting rows, not by reading the response.
- `GET /shows/{id}` reports a lapsed hold as `available`, matching what a claim would see.
- A reserve with no `hold_ttl_seconds` returns 201 `status: "confirmed"` with no `expires_at`, and the seats read `confirmed`. A reserve with one returns 201 `status: "held"` with `expires_at`, and the seats read `held`. Both are named tests: the hold path is now the less-travelled one and needs deliberate cover rather than cover by accident.
- A `hold_ttl_seconds` above the show maximum is clamped silently, and the returned `expires_at` reflects the clamped value, not the requested one.
- Unknown body fields on `POST /shows` are rejected 422; unknown body fields on a reserve are **ignored**, and a reserve carrying another principal's `user_id` returns **201 owned by the token subject** (ADR-028).
- `GET /nope` returns 404 `ROUTE_NOT_FOUND` in the standard envelope, not Starlette's `{"detail": "Not Found"}`. `DELETE /healthz` returns 405 `METHOD_NOT_ALLOWED` with an `Allow` header. `/healthz/` — a trailing slash, deliberately not redirected — returns `ROUTE_NOT_FOUND`.
- An unhandled exception produces **exactly one** `error` log line, carrying the request id and a stack. A second line with no request id means the exception escaped to `ServerErrorMiddleware` and ADR-024's boundary is not in place.
- Every error response carries the envelope and a request id matching the `X-Request-ID` header.
- An inbound `X-Request-ID` is adopted when a valid UUID and replaced when not, and the value reaches the `request_id` column of the rows the request wrote.

### Authorization probes

These are correctness requirements, not a security afterthought, and each is a distinct test:

- Reserve with `{"seats":["A1"],"user_id":"<other>"}` — **201**, and the reservation belongs to the token's subject. A 422 here would not demonstrate the property: the request has to act in order to show whom it acted as.
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

### Concurrent idempotency — REQ-024, REQ-025, REQ-026

Two to ten clients, identical key, identical body, fired together. Exactly one reservation exists. Every response is either the same 201/200 body or a 409 `IDEMPOTENCY_IN_PROGRESS`; no two different reservation ids are ever returned for one key.

Separately, each its own named test:

- Sequential retry of a success → **200**, never 201, with `Idempotent-Replay: true` and `seats.version` unchanged. The status assertion is load-bearing: a 201 replay would make the hot-seat 201 count something other than a count of claims (ADR-029).
- Same key, mutated body → 409 `IDEMPOTENCY_KEY_REUSED`, no seat moved, asserted on `seats.version`.
- **Same key, same body, different show → 409 `IDEMPOTENCY_KEY_REUSED`**, and no reservation on either show. Under the previous design this succeeded, and could replay the wrong show's reservation (ADR-021).
- **Same key after a decline → a genuine re-attempt, not a replayed decline.** Fire the key against a taken seat, get 409; free the seat; fire the same key again and assert **201**. Declines are not stored (ADR-020), so the second attempt must really run.
- A key left `in_progress` past the staleness window is reclaimed and the retry succeeds exactly once.
- While a duplicate is in its bounded wait, `db_pool_in_use` does not rise by one per waiter — the waiter releases its connection between polls (ADR-026). Fire more concurrent duplicates than the pool is wide and assert zero 503s; before that fix this test would exhaust the pool.

### Claim racing expiry — REQ-033, REQ-034

A hold about to lapse, raced by a competing claim. **No background task runs, because none exists** — if any assertion here depended on a worker, lazy-only expiry would not be the whole mechanism.

- The seat ends owned by exactly one principal, asserted on `seats` and on the single active `reservation_seats` row.
- The original holder's `confirm` returns 409 once its hold has lapsed — `RESERVATION_EXPIRED` whether or not the seat was re-claimed — and never takes the seat from a new owner.
- `cancel` of a lapsed hold returns 409 `RESERVATION_EXPIRED` and changes no seat.
- A cancel of a reservation whose seat has moved on affects zero rows.

### A claim against a lapsed, unswept hold — REQ-021, REQ-034, ADR-019

**The most important new test in the suite.** Create a hold with a short TTL, let it lapse, run no cleanup of any kind, then claim the same seat as a different principal.

```
assert status == 201                    # not 409
assert db: seats row owned by the new principal
assert db: exactly one reservation_seats row for that seat with released_at IS NULL
assert db: the old hold's row now has released_at set
assert logs: no uq_seat_active_claim violation at error level
assert metrics: superseded_claims_closed_total increased by 1
```

Without ADR-019's in-transaction closure, the legitimate winner receives a 409 from the backstop index and the `error` log line fires on a correct claim. Repeat for a multi-seat claim overlapping two separate lapsed holds, and concurrently with N claimers on the one lapsed seat — exactly one 201, zero 5xx, zero backstop violations.

### Reconciliation during load — REQ-013

A poller calls `GET /shows/{id}` continuously while the burst runs, asserting `available + held + confirmed == total_seats` on **every** sample. Sampling only after the burst is a materially weaker claim than the invariant makes, and this test is the one that catches a drifted effective-status expression.

### Cross-check

After every concurrency test: API state, database rows, and Prometheus counters must agree. A counter disagreeing with the database is a reportable defect even when the API looks correct.

One asymmetry is expected and must be asserted as such: a raw count of `reservation_seats` rows with `released_at IS NULL` can **exceed** the number of really-active claims, for exactly as long as a lapsed hold's seat goes unclaimed (RISK-007). The cross-check compares derived effective status, not open rows. A test that counts open rows and expects them to reconcile is asserting the thing this design deliberately does not provide.

### Negative controls

For each of four mechanisms — guarded claim, quota lock, idempotency key, and the superseded-row closure — a test runs against a deliberately broken variant and **must** fail. For the fourth, the broken variant simply omits the closure statement, and the lapsed-hold claim test must then fail with a 409 and a backstop violation. These are kept as explicit regression tests of the test suite itself, because a concurrency suite that cannot detect the bug it was written for is worse than none.

---

## Operational

- `/healthz` 200 with the database stopped; `/readyz` 503 naming `database`, never from a cache.
- `/metrics` parses as valid Prometheus text; every catalogued metric is present; no label carries a user id, seat label, or concrete path.
- Cold start: container from scratch reaches `/readyz` ready within the configured budget.
- Restart mid-burst: no double-sell, no stuck idempotency key, invariant intact afterwards. A key left `in_progress` by the kill is reclaimed on the next duplicate, lazily, with no worker involved.
- Graceful shutdown flushes the audit queue.
- A clean clone builds and runs via the documented command, in CI, with no manual step — this is the check for REQ-050, and the most common way an otherwise sound service fails to run elsewhere.

---

## The burst script

`burst/burst.py`, wrapped by `./burst.sh <BASE_URL>` and `make burst`. One command, any base URL, local or live.

### Phases

| Phase | Purpose |
|---|---|
| 0 — Warm | Poll `/readyz` until ready. Free tiers spin down; measuring a cold start as if it were load produces a meaningless result. |
| 1 — Provision | Authenticate as admin, create a fresh show with a configured seat count, mint the configured number of principals (guests by default — no credential setup, realistic for an on-sale). |
| 2 — Baseline | Capture `/metrics` and `GET /shows/{id}` before load. |
| 3 — Stampede | All principals reserve random seats concurrently, at the configured concurrency. Default reserves, so every success is a confirmation and no lapse can perturb the counts. |
| 4 — Hot-seat storm | All principals target one seat simultaneously, barrier-synchronized. Repeated for a configured number of hot seats. Default reserves, which is what makes "exactly one winner" a countable property (ADR-017). |
| 5 — Idempotent retries | A configured share of principals replay their exact request with the same key — expecting **200**, counted separately from 201; a smaller share replay the same key with a mutated body, expecting 409 `IDEMPOTENCY_KEY_REUSED`; a smaller share still replay the same key against a **different show**, also expecting 409. |
| 6 — Limit probe | One principal fires more parallel reserves than the limit permits. |
| 7 — Spoof probe | A reserve carrying another principal's id in the body; assert it returns **201** and the reservation belongs to the token's subject. A 422 fails this phase — the request must act to show whom it acted as. |
| 8 — Lifecycle | A dedicated share of principals reserve **with** `hold_ttl_seconds`, which is the only way the hold path gets exercised; a share of those confirm, a share cancel, and a share are left to lapse. Cancelled and lapsed seats are then re-reserved to prove they are cleanly re-bookable, and the re-reserve of a lapsed seat must return 201 rather than tripping the backstop index (ADR-019). |
| 9 — Reconcile | Re-read `GET /shows/{id}` and `/metrics`; check the invariant and metric agreement. |

A reconciliation poller runs throughout phases 3 through 8, sampling the invariant continuously.

### Output

```
BURST  base_url=https://…  show_id=…  seats=500  principals=2000  concurrency=500

phase            requests   201     200     409     422   429   5xx    p50     p99
stampede             2000   487       0    1513       0     0     0    14ms    91ms
hot_seat_storm       2500     5       0    2495       0     0     0    11ms   140ms
idempotent_retry      400     0      370      30       0     0     0     6ms    22ms
limit_probe            10     4       0       6       0     0     0     9ms    31ms
lifecycle             420   240     180       0       0     0     0     8ms    27ms

declines by reason
  seat_taken                4008
  per_user_limit               6
  idempotent_replay          370
  idempotency_key_reused      30

hot seats                 winners
  A12                           1    ✓
  B07                           1    ✓
  C15                           1    ✓

invariant samples          412 / 412 held                       ✓
final counts               available 21  held 14  confirmed 465  total 500   ✓
lapsed seats re-reserved    18 / 18                              ✓
superseded claims closed    18                                   ✓
metrics agree with API     yes                                   ✓
spoofed identity acted as token subject   yes                     ✓
5xx total                  0                                     ✓

VERDICT: PASS
```

### Contract

- Exit non-zero on **any** invariant violation, any 5xx, any hot seat with a winner count other than one, any metric disagreement, a failed identity-spoof probe, a 201 where a replay was expected, or any re-reserve of a lapsed seat that did not succeed. The script is a gate, not a report.
- **The exit contract is built and self-tested first** (ADR-030). Each violation condition is injected by a self-test that asserts the non-zero exit, and those self-tests need no running service — which is why the harness is a track from Stage 0 rather than the last thing written. A script that prints a distribution and exits zero on a violation is worse than no script, and that defect is only findable by deliberately injecting violations.
- Every parameter configurable by flag or environment variable — base URL, seat count, principal count, concurrency, hot-seat count, retry share, limit-probe size. Nothing hardcoded.
- Prints the actual outcome distribution. A script that only prints "PASS" is not evidence.
- Separates 429 from 409 in its own column, so a throttling artifact is never mistaken for a seat decline.
- Idempotent against a live service: creates its own show, touches nothing pre-existing.
- Uses a bounded connection pool with keep-alive. The goal is to load the service, not to exhaust local file descriptors and then report the client's failure as the server's.

---

## CI

On every push: lint, type check, unit, integration against a Postgres service container, concurrency suite at reduced scale, container build, and a smoke run of the built image from a clean checkout proving `/healthz` answers 200 (and `/readyz` once SEAT-009 lands it — the smoke target is liveness while readiness does not yet exist). The full-scale burst runs against the deployed URL manually and before each release, because it is slow and needs the live target.
