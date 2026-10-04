# Requirements

Each requirement is numbered, testable, and traced to a test. A requirement with no passing test is not satisfied, whatever the code looks like. The traceability table at the end is the current state: three requirements are partly met and one is not built.

Priority: **M** must, **S** should, **C** could.
Audience: who may invoke — `admin`, `user`, `guest`, `system`.

---

## Identity and access

**REQ-001** `M` `guest` — Register an account.
Given an unused email and a password meeting policy, when `POST /auth/register` is called, then a user row is created with role `user`, `is_guest=false`, the password stored as an Argon2 hash, and tokens are returned. A duplicate email returns 409 `EMAIL_TAKEN` without revealing anything further.

**REQ-002** `M` `guest` — Log in.
Given valid credentials, when `POST /auth/login` is called, then an access and a refresh token are returned. Invalid credentials return 401 `INVALID_CREDENTIALS` with the same latency and message whether the email exists or not.

**REQ-003** `M` `guest` — Obtain a guest identity.
When `POST /auth/guest` is called with no credentials, then a user row is created with `is_guest=true`, role `user`, and a short-lived access token is returned. A guest may reserve, confirm, cancel, and read its own reservations exactly as a registered user. Startup refuses to boot unless `GUEST_TOKEN_TTL_SECONDS > MAX_HOLD_TTL_SECONDS`, so no configuration can exist in which a guest's token always expires before the longest permitted hold (ADR-031; the residual per-request case is RISK-006).

**REQ-004** `M` `guest` — Upgrade a guest to a registered account.
Given a valid guest token and an unused email, when `POST /auth/upgrade` is called, then the **same** `user_id` gains credentials and `is_guest` becomes false, and every reservation already held by that id remains attached to it. A non-guest token returns 409 `ALREADY_REGISTERED`.

**REQ-005** `M` `user` — Identity is token-derived.
When any authenticated request includes a `user_id` in its body or query, then that value has no effect on the acting principal; the token subject is used. Verified by issuing a request whose body names another principal and asserting the created resource **is created** and belongs to the token's subject — a 422 would not demonstrate the property, because the request must act in order to show whom it acted as.

The guarantee is structural, not a validation rule: no request model anywhere declares an identity field, so there is nothing for a body value to bind to. It therefore survives any change to the unknown-field policy of ADR-028, under which non-admin endpoints ignore unknown fields and admin endpoints reject them.

**REQ-006** `M` `user` — Reject invalid tokens.
A missing, malformed, expired, or wrongly signed token returns 401 `UNAUTHENTICATED`. No route that requires a principal executes any business logic before this check.

**REQ-007** `M` `admin` — Admin-only routes refuse non-admins.
When a `user` or `guest` token calls an admin route, then 403 `FORBIDDEN` is returned and no state changes. Verified per admin route, not once.

**REQ-008** `M` `user` — Refresh an access token.
Given a valid refresh token, `POST /auth/refresh` returns a new access token. An expired, tampered or wrong-type token returns 401. Refresh is stateless, so there is no revocation: that is future scope.

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

**REQ-014** `C` `user` — List shows.
`GET /shows` returns a keyset-paginated summary, newest first. Page size is configured, not hardcoded, and bounded. It carries **no** availability counts: a list of shows must not scan every seat of every show, and exact availability is `GET /shows/{id}`.

---

## Reservation

**REQ-020** `M` `user` — Reserve seats.
Given a show and one or more free seat labels and an idempotency key, when `POST /shows/{id}/reserve` is called, then 201 is returned with `reservation_id`, `show_id`, `user_id`, `seats`, `amount_paise`, `currency`, `status`, `created_at`, and `expires_at` where applicable. `amount_paise` is an integer sum of the seats' prices.

Two outcomes, selected by one optional field (ADR-017):

| Request | Reservation | Seats | Response |
|---|---|---|---|
| no `hold_ttl_seconds` | `confirmed`, `confirmed_at` set, no expiry | `confirmed` | 201, `status: "confirmed"`, no `expires_at` |
| `hold_ttl_seconds` present | `held`, `hold_expires_at` set | `held` | 201, `status: "held"`, `expires_at` set |

Confirming is the default and the primary path. A `hold_ttl_seconds` above the show's maximum is **clamped silently** to that maximum, not rejected, and the response's `expires_at` reports the value actually applied.

**REQ-021** `M` `user` — No double-sell.
A seat active for one principal can never become active for another. Given N concurrent reserves for the same single seat, exactly one returns 201 and the other N−1 return 409 `SEAT_TAKEN`; zero return 5xx; exactly one database row records ownership.

Measured as a count of 201s, which is only meaningful because two design decisions keep other outcomes out of that count: a reserve confirms by default, so no lapsed hold can hand a legitimate second 201 for the same seat within one run (ADR-017), and a replay answers 200, so a retry is never counted as a claim (ADR-029).

**REQ-022** `M` `user` — All-or-nothing multi-seat.
Given a request for multiple labels where at least one is unavailable, then 409 `SEAT_TAKEN` is returned with the conflicting labels in `details.conflicts`, and **no** requested seat changes state. Holds under concurrency: a request that loses a race on its second seat must not leave its first seat held.

**REQ-023** `M` `user` — Per-user limit.
A principal may not hold or own more than the show's `per_user_limit` seats (default 4). A request that would exceed it returns 409 `PER_USER_LIMIT` and claims nothing. Holds under concurrency: ten parallel single-seat reserves against a limit of four end with exactly four held.

**REQ-024** `M` `user` — Idempotent reserve.
A repeated request with the same idempotency key and the same canonical request returns the original reservation's body with status **200** and the header `Idempotent-Replay: true`, and creates no additional reservation and no additional seat movement. A replay is never 201: a retry must not be countable as a second creation, since exactly-one-201-per-contested-seat is measured by counting status codes (ADR-029).

Only successes are stored under a key. A request whose outcome was a domain decline **releases** the key, so a later retry with that key genuinely re-attempts and may legitimately succeed (ADR-020). A key therefore produces at most one reservation, which is the property "reserves exactly once" states; it does not produce a frozen decline.

**REQ-025** `M` `user` — Key reuse with a different request.
The same idempotency key presented with a different canonical request returns 409 `IDEMPOTENCY_KEY_REUSED`, checked before any seat is touched. Canonicalization makes JSON key order and whitespace irrelevant, and seat-label order and duplication irrelevant.

The fingerprint covers exactly: the operation name, the **show id taken from the path**, the sorted de-duplicated seat labels, and `hold_ttl_seconds` when present (omitted entirely when absent, since "no TTL" and "TTL 120" are different operations). So the same key against a **different show** is 409 `IDEMPOTENCY_KEY_REUSED`, not a second reservation and not a wrong replay (ADR-021).

**REQ-026** `M` `user` — Concurrent duplicate keys.
Two concurrent requests bearing the same key produce **at most one** reservation. The loser waits a bounded interval, holding no database connection between polls (ADR-026), and then: replays the winner's result as 200 if the winner succeeded; re-attempts once if the winner declined and released the key; or returns 409 `IDEMPOTENCY_IN_PROGRESS` with `Retry-After` if the winner has not finished within the bound. In no interleaving do two different `reservation_id` values come back for one key.

**REQ-027** `M` `user` — Missing or malformed idempotency key.
A reserve without a key, or with one exceeding the configured length, returns 422 `VALIDATION_ERROR`. The key is accepted from the `Idempotency-Key` header or the request body; if both are present and differ, 422.

**REQ-028** `M` `user` — Reject unknown or duplicate labels.
A label not belonging to the show returns 404 `SEAT_NOT_FOUND`. A label repeated within one request returns 422. A request exceeding `per_user_limit` labels is declined before any claim.

**REQ-029** `M` `user` — Reserve against a closed show.
A reserve on a show that is not on sale returns 409 `SHOW_NOT_ON_SALE`.

---

## Lifecycle

**REQ-030** `M` `user` — Confirm a hold.
`POST /reservations/{id}/confirm` by the owner moves a `held` reservation and its seats to `confirmed` and clears the expiry. Confirming an already-confirmed reservation is idempotent and returns 200.

Confirming a cancelled reservation returns 409 `RESERVATION_CANCELLED`. Confirming a reservation whose hold has **lapsed** returns 409 `RESERVATION_EXPIRED` with `details.status`, whether or not its seats have since been re-claimed — the statement carries `hold_expires_at > now()`, so expiry is enforced by the same mechanism that enforces ownership and there is no background worker whose lag could make a lapsed hold promotable (ADR-022). Where a lapsed hold's seat has already been re-claimed by another principal, the confirm returns 409 and the new owner's seat is untouched.

**REQ-031** `M` `user` — Cancel a reservation.
`POST /reservations/{id}/cancel` by the owner releases the seats of a **confirmed** reservation or a live hold to `available`, sets status `cancelled`, and closes its `reservation_seats` rows (ADR-040). Cancelling an already-cancelled reservation is idempotent and returns 200, and never takes a seat back from whoever booked it since. Cancelling a reservation whose hold has lapsed returns 409 `RESERVATION_EXPIRED`: its seats are already effectively available, so there is nothing to release, and reporting the real state is more useful than a successful no-op.

**REQ-032** `M` `user` — Only the owner may act.
A confirm or cancel by any principal other than the reservation's owner returns 404 `RESERVATION_NOT_FOUND` — existence is not disclosed to a non-owner. Admin override, if enabled, is a separate route.

**REQ-033** `M` `system` — Holds expire.
A hold not confirmed within `hold_ttl_seconds` lapses. Its seats become claimable **immediately** via the claim predicate's expiry arm, which is the entire expiry mechanism — there is no sweeper, and none is required (ADR-017). Verified with no background task running at all.

From the lapse onward: `GET /shows/{id}` reports those seats `available`, the reservation reads as `expired`, the seats do not count against their former holder's per-user limit, and confirm and cancel on that reservation return 409 `RESERVATION_EXPIRED`. Stored seat and reservation rows are **not** rewritten; every reader derives effective status, which is exact at the instant of the read.

**REQ-034** `M` `system` — Release never resurrects.
A cancel or confirm can only affect seats the reservation still owns and still holds unexpired. A seat already confirmed to another principal is never returned to `available` by another reservation's release, and a former holder can never confirm a seat that has moved on. Verified by racing a cancel and a confirm against a competing claim across the expiry boundary.

A claim that supersedes a lapsed hold also closes that hold's `reservation_seats` row in the same transaction, so the legitimate winner never collides with the backstop index (ADR-019). Verified directly: a claim against a lapsed, unswept hold must return 201, not 409.

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

**REQ-042** `M` `system` — Metrics. **Partly met:** the counters, the availability gauge and `unhandled_exceptions_total` are exposed; the latency histogram, `db_pool_waiting`, the lock-wait histogram and the audit series are not ([17-future-scope.md](17-future-scope.md), item 4).
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
Zero 5xx on every domain path. Lock timeouts, serialization failures, deadlocks and driver errors on the claim path are translated to 4xx or retried within the request, never surfaced as 500. The one permitted 5xx is 503 `DATABASE_UNAVAILABLE`, raised only for a genuine fault: the database unreachable, the pool-acquire timeout exhausted, or a statement cancelled by `statement_timeout`. **Zero occurrences of it are required during a burst**, which makes pool sizing part of this requirement rather than a tuning detail. Measured directly by `unhandled_exceptions_total` remaining at zero.

A statement timeout is a fault and not a decline, which is only safe to assert because `DB_LOCK_TIMEOUT_MS` is held strictly below `DB_STATEMENT_TIMEOUT_MS` by a configured margin, validated at startup (ADR-027). Reversed, every hot-seat decline would arrive as a 503 and this requirement would fail for a configuration reason with no code defect.

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

| REQ | Test | Status |
|---|---|---|
| REQ-001, 002, 003, 006 | `tests/integration/test_auth.py` | covered |
| REQ-004 guest upgrade | `test_auth.py::test_a_guest_upgrade_keeps_the_same_user`, `::test_two_concurrent_upgrades_of_one_guest_have_one_winner` | covered; "reservations stay attached" follows from the unchanged id and is not asserted |
| REQ-005 identity is token-derived | `test_reserve.py::test_a_reserve_confirms_outright_and_is_owned_by_the_token_subject` | covered |
| REQ-007 admin-only | `test_shows.py::test_only_an_admin_may_create_a_show` | covered (one admin route exists) |
| REQ-008 refresh | `test_auth.py::test_login_issues_a_refresh_token_that_only_refreshes` | covered; stateless, so "revoked or rotated" has no mechanism (open question 2) |
| REQ-010, 011, 012 | `tests/integration/test_shows.py` | covered |
| REQ-013 reconciliation | `tests/concurrency/test_reserve_races.py::test_reconciliation_holds_while_a_burst_is_in_flight`, `burst/` | covered |
| REQ-014 list shows | `test_shows.py::test_the_show_list_is_keyset_paginated_newest_first` | covered |
| REQ-020, 022, 027, 028 | `tests/integration/test_reserve.py` | covered |
| REQ-021 no double-sell | `test_reserve_races.py::test_hot_seat_has_exactly_one_winner_and_no_5xx` | covered |
| REQ-023 per-user limit | `test_reserve_races.py::test_one_principal_never_exceeds_the_limit` | covered |
| REQ-024, 025 | `test_reserve.py::test_a_replay_answers_200_with_the_original_body`, `tests/unit/test_canonical_json.py` | covered |
| REQ-026 concurrent duplicates | `test_reserve_races.py::test_one_key_fired_concurrently_reserves_once`, `tests/integration/test_reserve_edges.py` (timeout, stale takeover) | covered |
| REQ-029 not on sale | `test_reserve_edges.py::test_a_show_that_is_not_on_sale_declines` | covered; no API takes a show off sale, so the test sets the status directly |
| REQ-030, 031, 032, 035 | `tests/integration/test_lifecycle.py` (including cancel of a confirmed booking and re-booking), `test_reserve_edges.py::test_only_the_owner_may_confirm_or_read`, the burst's release probe | covered |
| REQ-033 holds expire | `test_reserve_races.py::test_a_lapsed_hold_is_claimable_with_no_sweeper`, `test_reserve_edges.py::test_a_lapsed_hold_reads_expired_frees_the_limit_and_cannot_be_cancelled` | covered |
| REQ-034 release never resurrects | `test_lifecycle.py` (repeat cancel after re-booking), lapsed-claim test above | **partial** — the cancel/confirm race against a competing claim across the expiry boundary is not tested |
| REQ-036 read own reservations | `test_lifecycle.py::test_a_principal_lists_only_their_own_reservations` | covered |
| REQ-040, 044, 045 | `tests/integration/test_ops.py`, `test_request_context.py`, `test_errors.py`, `test_reserve_edges.py` (row stamping) | covered |
| REQ-041 readiness | `tests/integration/test_readyz.py` | covered |
| REQ-042 metrics | `tests/integration/test_metrics.py` | **partial** — no latency histogram, `db_pool_waiting`, `seat_claim_lock_wait_seconds` or audit series |
| REQ-043 metrics reconcile | `test_metrics.py`, `burst/` | covered |
| REQ-046 audit | `tests/integration/test_admin.py` | covered: a full buffer drops and counts, and every request still succeeds |
| REQ-047 rate limiting | `tests/integration/test_rate_limit.py` | covered; `auth` is keyed by address only, not address and email (ADR-034) |
| REQ-048 no 5xx | `test_reserve_edges.py::test_a_lock_timeout_is_a_409_never_a_500`, race tests, `burst/` | covered for lock timeout; deadlock and backstop translation are not injected by a test |
| REQ-049 cold start | `burst/` warms `/readyz` before measuring | covered by the script; cold-start time is not measured |
| REQ-050 clean checkout | `docker compose up --build`, run locally | **partial** — the CI container job has never run |
| REQ-060 integer money | `test_shows.py` (float price is 422; tiered exact sum) | **partial** — no static float check |

---

## Open questions

Non-blocking; each carries a working default so no stage stalls.

1. **Admin bootstrap** — **resolved, built:** seeded from env credentials at startup, created only if absent, with a loud log line.
2. **Refresh-token revocation** — **built stateless**; a revocation list is future scope.
3. **Admin override on cancel** — may an admin cancel another principal's hold? Default: no route exists until asked for.
4. **Seat-level pricing tiers** — `seats.price_paise` nullable and inheriting the show price is already in the schema; tier naming and a tier table are deferred until a requirement needs them.
5. **Guest token lifetime versus hold TTL** — **resolved.** `GUEST_TOKEN_TTL_SECONDS > MAX_HOLD_TTL_SECONDS`, validated at startup (ADR-031, and stated as an acceptance clause on REQ-003). `MAX_HOLD_TTL_SECONDS` is the right quantity because it is the longest hold the service will ever issue; there is no single "the hold TTL" to add a margin to. The check is necessary, not sufficient — a token minted shortly before a maximum-length hold can still lapse first — and that residual is RISK-006, not something a startup check can see. It costs the guest one retry and loses no seat, which is why it is accepted rather than fixed by coupling the claim path to token internals.
