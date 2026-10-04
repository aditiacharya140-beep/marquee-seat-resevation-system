# Future scope

Everything that was designed, or found to be needed, and is **not in the code**. The other documents in this set describe the service as it is; this one is the only place that describes what it is not yet.

Each item says what it is, why it is not built, and what picking it up involves. Items are ordered by how much they matter, not by size.

---

## 1. Something a guest cannot mint for free

**The gap.** The per-user seat limit is per principal, a guest principal costs nothing to create, and a reserve confirms with no payment step. Rate limiting bounds guest creation to one a second per client address (ADR-034), which narrows this and does not close it: a patient client, or one with many addresses, still accumulates seats (RISK-014).

**What closes it.** A step that cannot be repeated for free — payment capture between hold and confirm, or a verified email or phone before a principal may reserve. `hold_ttl_seconds` already gives the hold/confirm split a payment step would attach to; making a hold the default again for unverified principals is the smallest version.

**Also here:** a minimum hold TTL above `DB_LOCK_TIMEOUT_MS`, so a hold cannot be committed already lapsed (RISK-016); and keying the `auth` rate class by address *and* email, which needs the limiter to see the body.

## 2. Audit trail — REQ-046

**What it is.** A queryable record of every request: `audit_log (id BIGSERIAL, request_id, occurred_at, method, path, route, status_code, duration_ms, user_id, is_guest, outcome_code, show_id, seat_labels, idempotency_key, client_ip)`, indexed on `occurred_at DESC`, on `request_id`, and partially on `outcome_code`. No foreign keys: a check per insert adds contention for nothing, and a row naming a deleted user is still evidence.

**The write path that was designed, and must be kept if it is built:**

- An ASGI middleware, innermost, builds one record per request and calls `put_nowait` on a bounded in-memory queue. It never awaits and never reads the body.
- A full queue **drops** the record, counts it (`audit_records_dropped_total`) and logs `audit_queue_saturated`. Losing an audit row is an inconvenience; stalling a booking behind one is a defect.
- A writer task drains up to a batch size or a flush interval and inserts with one multi-row statement on a **dedicated connection outside the request pool**, so a saturated pool cannot stall audit and a slow flush cannot starve requests.
- The lifespan drains the queue on shutdown with a bounded timeout.

**Why it is not built.** Cut for the deadline (ADR-032). Structured logs carry `request_id`, and every row a request writes is stamped with it, so the forensic question "what did this request do" is answerable today from logs plus rows. What is missing is the aggregate question — "every declined reserve on this show in the last ten minutes" — without a log search.

**Picking it up.** A second Alembic revision for the table; `repositories/audit_repo.py`, `middleware/audit.py`, `workers/audit_writer.py`; settings for queue size, batch size and flush interval; `audit_queue_depth`, `audit_records_written_total`, `audit_records_dropped_total`. The proving test sizes the queue to 1, fires 200 requests, and asserts every one still succeeds while the drop counter rises. About one sitting.

## 3. Monitoring view

A read-only admin page over `audit_log`: request rate, status distribution, declines by code, recent failures linking to their request ids. Depends entirely on item 2. Strictly off the request path. Optional even then.

## 4. Metrics that are specified but not exposed

| Metric | What it would show |
|---|---|
| `http_requests_total`, `http_request_duration_seconds`, `http_requests_in_flight` | Latency and volume by route template, method and status — needs a `MetricsMiddleware` between the access log and the rate limiter |
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
- **Principal in the request context**, so the access-log line carries `user_id` without the route passing it.

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

Not built: a spoofed-identity phase, a confirm/cancel/re-book lifecycle phase, the same key with a mutated body, more than one hot seat, the per-phase table, a self-test that injects each violation and asserts the non-zero exit, `make burst`, and a run at the scale the design is sized for — the largest live run is the script's default, 400 buyers and 150 on one seat.

## 11. Operating at scale

- **A connection pooler** in transaction mode is the step after the pool is exhausted; note that session-level startup parameters (how the guards are applied, LEARN-014) need care behind one.
- **Exact, cross-instance rate limiting** needs a shared store; buckets are per process (RISK-003).
- **Migrations as a separate release phase**, once there is more than one instance to race (RISK-005).
- **A hash-pinned lockfile**; direct dependencies are pinned exactly, transitive ones are not (RISK-008).
- **Sharding by show**, since shows are independent, is the step after the primary's write throughput.

## 12. Out of scope by decision

General admission (a capacity counter is a different mechanism with its own argument), read replicas, caching show state, multi-region writes, and a virtual waiting room. Reasons are in [11-scalability.md](11-scalability.md).
