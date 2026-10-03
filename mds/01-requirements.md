# Requirements

Each requirement is numbered, testable, and traced to a test. A requirement with no passing test is not satisfied, whatever the code looks like.

Priority: **M** must, **S** should, **C** could.
Audience: who may invoke — `admin`, `user`, `guest`, `system`.

---

## Identity and access

**REQ-001** `M` `guest` — Register an account.
Given an unused email and a password meeting policy, when `POST /auth/register` is called, then a user row is created with role `user`, `is_guest=false`, the password stored as an Argon2 hash, and tokens are returned. A duplicate email returns 409 `EMAIL_TAKEN` without revealing anything further.

**REQ-002** `M` `guest` — Log in.
Given valid credentials, when `POST /auth/login` is called, then an access and a refresh token are returned. Invalid credentials return 401 `INVALID_CREDENTIALS` with the same latency and message whether the email exists or not.

**REQ-003** `M` `guest` — Obtain a guest identity.
When `POST /auth/guest` is called with no credentials, then a user row is created with `is_guest=true`, role `user`, and a short-lived access token is returned. A guest may reserve, confirm, cancel, and read its own reservations exactly as a registered user.

**REQ-004** `M` `guest` — Upgrade a guest to a registered account.
Given a valid guest token and an unused email, when `POST /auth/upgrade` is called, then the **same** `user_id` gains credentials and `is_guest` becomes false, and every reservation already held by that id remains attached to it. A non-guest token returns 409 `ALREADY_REGISTERED`.

**REQ-005** `M` `user` — Identity is token-derived.
When any authenticated request includes a `user_id` in its body or query, then that value has no effect on the acting principal; the token subject is used. Verified by issuing a request whose body names another principal and asserting the created resource belongs to the token's subject.

**REQ-006** `M` `user` — Reject invalid tokens.
A missing, malformed, expired, or wrongly signed token returns 401 `UNAUTHENTICATED`. No route that requires a principal executes any business logic before this check.

**REQ-007** `M` `admin` — Admin-only routes refuse non-admins.
When a `user` or `guest` token calls an admin route, then 403 `FORBIDDEN` is returned and no state changes. Verified per admin route, not once.

**REQ-008** `M` `user` — Refresh an access token.
Given a valid refresh token, `POST /auth/refresh` returns a new access token. A refresh token that has been revoked or rotated returns 401.

---

## Catalogue

**REQ-010** `M` `admin` — Create a show.
Given `{name, seats[], price_paise}`, when `POST /shows` is called by an admin, then a show is created with one seat row per label, all `available`, and the show plus its seats are returned with `total_seats` equal to the label count. Optional fields `per_user_limit` (default 4), `hold_ttl_seconds` (default from config), `event_kind`, and per-seat pricing are accepted.

**REQ-011** `M` `admin` — Reject an invalid show.
Duplicate labels in `seats[]`, an empty `seats[]`, a non-integer or negative `price_paise`, or a label exceeding the configured length return 422 `VALIDATION_ERROR` naming the offending field. No partial show is created.

**REQ-012** `M` `user` — Read show state.
`GET /shows/{id}` returns per-seat status (`available` / `held` / `confirmed`) and counts. A seat whose hold has lapsed reports `available`, consistent with what a claim would see. An unknown id returns 404 `SHOW_NOT_FOUND`.

**REQ-013** `M` `system` — Reconciliation invariant.
At every instant, including during a burst, `available + held + confirmed == total_seats` for every show. Verified by sampling `GET /shows/{id}` concurrently with load, not only after it.

**REQ-014** `C` `user` — List shows. **Deferred (ADR-015)** — nothing depends on it; revisit after Stage 7.
`GET /shows` returns a paginated summary with availability counts. Page size is configured, not hardcoded, and bounded.

---

## Reservation

**REQ-020** `M` `user` — Reserve seats.
Given a show and one or more free seat labels and an idempotency key, when `POST /shows/{id}/reserve` is called, then a reservation is created in status `held` with `hold_expires_at`, the named seats become `held` for the principal, and 201 is returned with `reservation_id`, `show_id`, `user_id`, `seats`, `amount_paise`, `status`, and `expires_at`. `amount_paise` is an integer sum of the seats' prices.

**REQ-021** `M` `user` — No double-sell.
A seat active for one principal can never become active for another. Given N concurrent reserves for the same single seat, exactly one returns 201 and the other N−1 return 409 `SEAT_TAKEN`; zero return 5xx; exactly one database row records ownership.

**REQ-022** `M` `user` — All-or-nothing multi-seat.
Given a request for multiple labels where at least one is unavailable, then 409 `SEAT_TAKEN` is returned with the conflicting labels in `details.conflicts`, and **no** requested seat changes state. Holds under concurrency: a request that loses a race on its second seat must not leave its first seat held.

**REQ-023** `M` `user` — Per-user limit.
A principal may not hold or own more than the show's `per_user_limit` seats (default 4). A request that would exceed it returns 409 `PER_USER_LIMIT` and claims nothing. Holds under concurrency: ten parallel single-seat reserves against a limit of four end with exactly four held.

**REQ-024** `M` `user` — Idempotent reserve.
A repeated request with the same idempotency key returns the original reservation with the original status code and body, flagged as a replay, and creates no additional reservation and no additional seat movement.

**REQ-025** `M` `user` — Key reuse with a different body.
The same idempotency key presented with a different canonical request body returns 409 `IDEMPOTENCY_KEY_REUSED`, checked before any seat is touched. Canonicalization makes key order and whitespace irrelevant.

**REQ-026** `M` `user` — Concurrent duplicate keys.
Two concurrent requests bearing the same key produce exactly one reservation. The loser waits a bounded interval and returns the winner's result; if the winner has not completed within that bound, 409 `IDEMPOTENCY_IN_PROGRESS` is returned and no second reservation exists.

**REQ-027** `M` `user` — Missing or malformed idempotency key.
A reserve without a key, or with one exceeding the configured length, returns 422 `VALIDATION_ERROR`. The key is accepted from the `Idempotency-Key` header or the request body; if both are present and differ, 422.

**REQ-028** `M` `user` — Reject unknown or duplicate labels.
A label not belonging to the show returns 404 `SEAT_NOT_FOUND`. A label repeated within one request returns 422. A request exceeding `per_user_limit` labels is declined before any claim.

**REQ-029** `M` `user` — Reserve against a closed show.
A reserve on a show that is not on sale returns 409 `SHOW_NOT_ON_SALE`.

---

## Lifecycle

**REQ-030** `M` `user` — Confirm a hold.
`POST /reservations/{id}/confirm` by the owner moves a `held` reservation and its seats to `confirmed` and clears the expiry. Confirming an already-confirmed reservation is idempotent and returns 200. Confirming an expired or cancelled reservation returns 409 with the terminal status.

**REQ-031** `M` `user` — Cancel a hold.
`POST /reservations/{id}/cancel` by the owner releases a `held` reservation's seats to `available` and sets status `cancelled`. Cancelling an already-cancelled reservation is idempotent.

**REQ-032** `M` `user` — Only the owner may act.
A confirm or cancel by any principal other than the reservation's owner returns 404 `RESERVATION_NOT_FOUND` — existence is not disclosed to a non-owner. Admin override, if enabled, is a separate route.

**REQ-033** `M` `system` — Holds expire.
A hold not confirmed within `hold_ttl_seconds` lapses. Its seats become claimable immediately via the claim predicate, and the sweeper subsequently sets them `available` and the reservation `expired`.

**REQ-034** `M` `system` — Release never resurrects.
A cancel, confirm, or sweep can only affect seats the reservation still owns. A seat already confirmed to another principal is never returned to `available` by another reservation's release. Verified by racing a cancel against the expiry sweeper and a competing claim.

**REQ-035** `M` `user` — Released seats are cleanly re-bookable.
A seat released by cancel or expiry can be reserved by any principal with no residual state, and the resulting reservation is indistinguishable from a first booking.

**REQ-036** `S` `user` — Read own reservations.
`GET /reservations` returns the principal's reservations; `GET /reservations/{id}` returns one it owns, 404 otherwise.

---

## Operations

**REQ-040** `M` `system` — Liveness.
`GET /healthz` returns 200 whenever the process is running, touches no dependency, and completes in single-digit milliseconds.

**REQ-041** `M` `system` — Readiness fails closed.
`GET /readyz` executes a real query against the database. With the database unreachable it returns 503 with the failing dependency named. It never reports ready on a cached result.

**REQ-042** `M` `system` — Metrics.
`GET /metrics` exposes Prometheus text format including: reservations confirmed (counter), reservations declined by reason (counter, labelled `seat_taken` / `per_user_limit` / `idempotent_replay` / `show_not_on_sale` / `lock_timeout`), seats available (gauge, labelled by show), request latency (histogram by route and status), audit queue depth and drops, and three operational series without which other requirements cannot be verified: `unhandled_exceptions_total` (the direct measurement of REQ-048), `db_pool_waiting` (the precursor to a 503), and `seat_claim_lock_wait_seconds` (hot-seat contention approaching `lock_timeout`).

**REQ-043** `M` `system` — Metrics reconcile.
Counter and gauge values agree with API state and with the database after a burst, within the gauge's refresh interval.

**REQ-044** `M` `system` — Request correlation.
Every request is assigned a UUID request id — taken from `X-Request-ID` when it is a valid UUID, otherwise minted. It appears on every log line for that request, in the response header, in the error envelope, and in the `request_id` column of every row the request writes.

**REQ-045** `M` `system` — Structured logs.
Every log line is single-line JSON with `ts`, `level`, `event`, `request_id`. No secrets, tokens, or password material is ever logged.

**REQ-046** `M` `system` — Audit without backpressure.
Every request is recorded to an audit table through a bounded in-memory queue drained by a batched writer. The request path never blocks on the audit write. Queue saturation drops records, increments a drop counter, and does not degrade request handling.

**REQ-047** `M` `system` — Rate limiting.
Per-principal limits apply, configured per route class via environment variables with no redeploy required to change a ceiling. The reserve path's ceiling is set so that a legitimate on-sale stampede of distinct principals is never throttled. Exceeding a limit returns 429 with `Retry-After` and is counted.

**REQ-048** `M` `system` — No 5xx on domain paths.
Zero 5xx on every domain path. Lock timeouts, serialization failures, and driver errors on the claim path are translated to 4xx or retried within the request, never surfaced as 500. The one permitted 5xx is 503 `DATABASE_UNAVAILABLE` when the database is genuinely unreachable; **zero occurrences of it are required during a burst**, which makes pool sizing part of this requirement rather than a tuning detail. Measured directly by `unhandled_exceptions_total` remaining at zero.

**REQ-049** `M` `system` — Cold start.
After idle spin-down, the first request causes the service to come up and report healthy, and the burst script warms the target before measuring.

**REQ-050** `M` `system` — Clean checkout runs.
A fresh clone builds and runs via the documented container command with no undocumented manual step, and the same image is what deploys.

---

## Money

**REQ-060** `M` `system` — Integer minor units only.
Every monetary value is an integer count of paise, stored as `BIGINT`, transported as a JSON integer, and never converted to or through a float at any layer. Verified by a static check for float arithmetic on money fields and by asserting exact totals on large quantities.

---

## Traceability

| REQ | Acceptance covered by | Test | Status |
|---|---|---|---|
| REQ-001 – REQ-008 | auth suite | `tests/integration/test_auth.py` | pending |
| REQ-010 – REQ-014 | show suite | `tests/integration/test_shows.py` | pending |
| REQ-020, 027, 028, 029 | reserve contract | `tests/integration/test_reserve.py` | pending |
| REQ-021, 022 | hot-seat storm, multi-seat race | `tests/concurrency/test_double_sell.py` | pending |
| REQ-023 | parallel limit breach | `tests/concurrency/test_user_limit.py` | pending |
| REQ-024 – REQ-026 | idempotency, concurrent keys | `tests/concurrency/test_idempotency.py` | pending |
| REQ-030 – REQ-036 | lifecycle, expiry race | `tests/concurrency/test_lifecycle.py` | pending |
| REQ-005, 007, 032 | authorization probes | `tests/integration/test_authz.py` | pending |
| REQ-013, 043, 048 | burst reconciliation | `burst/` + `tests/concurrency/test_reconciliation.py` | pending |
| REQ-040 – REQ-047, 049 | operational suite | `tests/integration/test_ops.py` | pending |
| REQ-050 | container smoke | CI job | pending |
| REQ-060 | money invariants | `tests/unit/test_money.py` | pending |

---

## Open questions

Non-blocking; each carries a working default so no stage stalls.

1. **Admin bootstrap** — how does the first admin exist? Default: seeded from env credentials at startup, created only if absent, with a loud log line.
2. **Refresh-token revocation** — stored and revocable, or short-lived and stateless? Default: stateless with a short lifetime; add a revocation table only if a requirement needs it.
3. **Admin override on cancel** — may an admin cancel another principal's hold? Default: no route exists until asked for.
4. **Seat-level pricing tiers** — `seats.price_paise` nullable and inheriting the show price is already in the schema; tier naming and a tier table are deferred until a requirement needs them.
5. **Guest token lifetime versus hold TTL** — a guest token must outlive a hold or the holder cannot confirm. Default: guest token lifetime is at least `hold_ttl_seconds` plus a configured margin, enforced at startup.
