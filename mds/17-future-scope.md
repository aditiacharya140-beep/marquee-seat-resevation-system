# Future scope

Everything that was designed, or found to be needed, and is **not in the code**. The other documents in this set describe the service as it is; this one is the only place that describes what it is not yet.

Each item says what it is, why it is not built, and what picking it up involves. Items are ordered by how much they matter, not by size.

---

## 1. Something a guest cannot mint for free

**The gap.** The per-user seat limit is per principal, a guest principal costs nothing to create, and a reserve confirms with no payment step. Rate limiting bounds guest creation to one a second per client address (ADR-034), which narrows this and does not close it: a patient client, or one with many addresses, still accumulates seats (RISK-014).

**What closes it.** A step that cannot be repeated for free — payment capture between hold and confirm, or a verified email or phone before a principal may reserve. `hold_ttl_seconds` already gives the hold/confirm split a payment step would attach to; making a hold the default again for unverified principals is the smallest version.

**Also here:** a minimum hold TTL above `DB_LOCK_TIMEOUT_MS`, so a hold cannot be committed already lapsed (RISK-016); and keying the `auth` rate class by address *and* email, which needs the limiter to see the body.

## 2. Audit trail and admin console — built; what remains

Both exist (ADR-038). Still owed:

- **Retention.** Nothing purges `audit_log`; it grows by one row per request. Partitioning by `occurred_at` with a retention window is the plan.
- **Aggregates off the request pool.** The console's queries borrow a pooled connection. They are bounded by window and row limit, but under a burst they compete with bookings for the pool; a second small pool or the writer's connection would isolate them.
- **A durable log view.** The console shows this process's last `LOG_BUFFER_MAX` lines from memory, which a restart empties. Shipping logs to a store is the real answer.
- **An unpublished admin.** The demo's admin sign-in is in the README by decision (ADR-037). A real deployment needs the opposite, and admin actions audited under `/admin` rather than exempted.
- **Browser coverage.** The console has been exercised through its API and one local run, not across browsers.

## 3. (merged into item 2)

## 4. Metrics that are specified but not exposed

| Metric | What it would show |
|---|---|
| `http_requests_total`, `http_request_duration_seconds`, `http_requests_in_flight` | Latency and volume by route template, method and status as Prometheus series. Today the same numbers come from the audit trail, in the admin console |
| `db_pool_size`, `db_pool_in_use`, `db_pool_waiting` | Pool saturation, the precursor to a 503 |
| `seat_claim_lock_wait_seconds` | Hot-seat queues approaching `lock_timeout` |
| `seats_held`, `seats_confirmed`, `seats_total` | The other three terms of the invariant, per show. Today only `seats_available` is a gauge; `GET /shows/{id}` is the exact source for all four |
| `db_query_duration_seconds` | Per-operation query latency |

REQ-042 names the latency histogram, `db_pool_waiting` and the lock-wait histogram, so that requirement is only partly met. Each of the two pool/lock series is the early warning for an alert that today can only fire after the fact.

## 5. One log line for an unhandled exception

Starlette's `ServerErrorMiddleware` re-raises after the 500 handler has answered, so the ASGI server logs the trace a second time, outside the request context and so without the request id (LEARN-010). The designed fix is an exception-boundary middleware immediately inside `RequestContextMiddleware` that catches, logs once and answers, so nothing reaches `ServerErrorMiddleware` (ADR-024). The envelope half of the same ticket (SEAT-066) is built; this half is not.

## 6. Show lifecycle

- **Taking a show off sale.** `shows.status` has `draft`, `on_sale` and `closed`, and a reserve against anything but `on_sale` declines 409 `SHOW_NOT_ON_SALE` — but every show is created `on_sale` and no endpoint changes it.
- **Sale windows.** `shows.sales_open_at` and `sales_close_at` exist as columns and are never read.
- **Admin override** on another principal's reservation: no route exists, by design, until one is asked for.

## 7. Idempotency housekeeping

- **Retention purge.** Keys carry `expires_at` and `ix_idem_expiry` exists, but nothing deletes expired keys. The statement is in the runbook of [13-deployment.md](13-deployment.md); a `purge_expired` repository method and something to schedule it are not written (RISK-007).
- **Waiter backoff.** A duplicate waiting on an in-progress key polls at a fixed interval; under pool pressure that adds load when the pool is shortest (RISK-015).
- **Serialization-failure retry.** `40001` is specified as "retry once in-request, then 409". Nothing runs above `READ COMMITTED`, so it cannot currently occur, and no retry is written.

## 8. Auth hardening

- **Refresh-token revocation.** Refresh is stateless; a `jti` denylist is what "log out everywhere" or "this token was stolen" would need.
- **Argon2 parameters from configuration.** The library defaults are used; only the minimum password length and the hashing pool width are settings.

## 9. Tests that are owed

| Test | What it would prove |
|---|---|
| Negative controls as a module | The race tests were run once, by hand, against a claim predicate replaced with `true`, and four of six failed. As an automated module with injected broken variants (read-then-write claim, unlocked quota, key claimed inside T2), a control that stops detecting its bug would break the build |
| Cancel or confirm racing a competing claim across the expiry boundary | REQ-034 in full. The adversarial review exercised it by randomized churn and found nothing (LEARN-017); there is no permanent test |
| Deadlock and backstop-violation translation, injected | That those two rows of the driver-error table answer 409 — only the lock-timeout row is injected today |
| Cold start and restart mid-load | `/readyz` from a container started from scratch; no double-sell and no stuck key across a restart |
| Static checks | No float arithmetic on a `_paise` identifier; each effective-status fragment written in exactly one file; no `asyncpg` import outside `app/db` and `app/repositories` |
| Authorization probes, parameterized over the route table | So a protected route added later without a probe fails a count assertion |
| The four LEARN-002 probes | "Loser blocks then re-evaluates" and "winner rolls back, blocked claimer wins" as direct two-connection tests; the other two are covered |
| CI | The workflow has never run on a real runner: GitHub Actions is not enabled for the repository |

## 10. Burst script

Built: warm-up, own show, stampede with reconciliation sampled mid-flight, barrier-released hot seat, one key fired concurrently, limit probe, reconciliation against the API, the 201 bodies and `/metrics`, non-zero exit on violation.

Also built: a spoofed-identity check and a cancel-and-re-book check. Not built: a hold-then-confirm phase, the same key with a mutated body, more than one hot seat, the per-phase table, a self-test that injects each violation and asserts the non-zero exit, `make burst`, and a run at the scale the design is sized for — the largest live run is the script's default, 400 buyers and 150 on one seat.

## 11. Operating at scale

- **A connection pooler** in transaction mode is the step after the pool is exhausted; note that session-level startup parameters (how the guards are applied, LEARN-014) need care behind one.
- **Exact, cross-instance rate limiting** needs a shared store; buckets are per process (RISK-003).
- **Migrations as a separate release phase**, once there is more than one instance to race (RISK-005).
- **A hash-pinned lockfile**; direct dependencies are pinned exactly, transitive ones are not (RISK-008).
- **Sharding by show**, since shows are independent, is the step after the primary's write throughput.

## 12. Out of scope by decision

General admission (a capacity counter is a different mechanism with its own argument), read replicas, caching show state, multi-region writes, and a virtual waiting room. Reasons are in [11-scalability.md](11-scalability.md).
