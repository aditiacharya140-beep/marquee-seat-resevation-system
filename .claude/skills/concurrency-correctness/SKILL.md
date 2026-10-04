---
name: concurrency-correctness
description: The non-negotiable correctness rules for seat allocation under contention — the atomic claim mechanism, lock ordering, the reconciliation invariant, idempotency semantics, per-user limit enforcement, and the Postgres behaviours the proofs rest on. Load before touching seat claiming, reservations, idempotency, expiry, or any concurrency test.
---

# Concurrency correctness

These are invariants, not preferences. Code that violates one is wrong even if every test passes.

## The five invariants

1. **No double-sell.** A seat active for one principal (held and unexpired, or confirmed) can never become active for another. Exactly one winner per contested seat.
2. **No 5xx for a domain outcome.** Losing a race, exceeding a limit, replaying a key — all 4xx with a specific code. A 500 under load is a failure of the service, not of the request.
3. **Reconciliation.** `available + held + confirmed == total_seats` at every instant, during and after a burst.
4. **Exactly-once per idempotency key.** Same key reserves once; a retry returns the original result; the same key with a different body is rejected.
5. **Token-derived identity.** The acting principal is the token's subject. Ownership is checked against it. A body field can never change who acts.

## The atomic decision

A read-then-write (`SELECT status; if available: UPDATE`) double-sells under load. It is forbidden regardless of how it is wrapped.

**Single-seat claim** — one guarded conditional `UPDATE`, which is the whole decision:

```sql
UPDATE seats
   SET status = 'held', held_by = $user, reservation_id = $res,
       hold_expires_at = now() + $ttl, version = version + 1,
       updated_at = now(), request_id = $rid
 WHERE show_id = $show
   AND label = $label
   AND (status = 'available' OR (status = 'held' AND hold_expires_at <= now()))
RETURNING id, label;
```

Zero rows returned means the seat was taken — a clean 409. One row means this transaction owns it. Under `READ COMMITTED`, a concurrent `UPDATE` on the same row blocks, then re-evaluates the `WHERE` clause against the committed version, so a loser sees the predicate fail and affects nothing. There is no window between the check and the write because they are the same statement.

**Verified, not assumed.** All four behaviours were probed directly against PostgreSQL 16.15 before any application code existed (LEARN-002 in `mds/99-ledger.md`):

| Probe | Result |
|---|---|
| Loser blocks then re-evaluates | `UPDATE 0`, row stays with the winner |
| 50 concurrent claimers, one seat | 1 winner, 49 losers, **0 errors** |
| Winner rolls back instead of committing | Blocked claimer then wins — correct, a rolled-back claim never happened |
| Lapsed hold, no sweeper running | Claimable immediately |

The zero-errors result is the load-bearing one: a loser is a *decision* (zero rows affected), not an exception to catch and translate. That is what makes "no 5xx for a domain outcome" achievable rather than aspirational.

These four are the minimum regression set for any change to the claim predicate. Reproduce them as automated tests rather than trusting a one-off manual result.

**Multi-seat claim** — all-or-nothing, in one statement, locking in a deterministic order:

```sql
WITH candidate AS (
    SELECT id FROM seats
     WHERE show_id = $show AND label = ANY($labels)
       AND (status = 'available' OR (status = 'held' AND hold_expires_at <= now()))
     ORDER BY label
       FOR UPDATE
)
UPDATE seats s SET ... FROM candidate c WHERE s.id = c.id
RETURNING s.id, s.label, s.price_paise;
```

If the row count returned is less than the number of labels requested, the transaction rolls back and the request declines 409 listing the conflicts. Nothing is ever partially claimed.

**Lock ordering, and why there is no deadlock.** Every claiming transaction takes locks in exactly this order:

1. The `(user_id, show_id)` quota row for its own principal.
2. Seat rows in ascending `label` order (`ORDER BY label` inside `FOR UPDATE`).

Two transactions for different principals never contend on step 1 and always agree on the order in step 2. Two transactions for the same principal serialize at step 1. No cycle is constructible, so no deadlock.

`SKIP LOCKED` must **not** appear anywhere — skipping a momentarily locked row would decline a seat that is still available, and with no sweeper it has no legitimate use.

`lock_timeout` is set per claiming transaction. A timeout is translated to a 409 with its own metric label, never a 500 and never an indefinite wait.

## Belt and braces

The guarded update is the mechanism. A partial unique index is the backstop that makes a second active claim physically impossible even if the predicate were ever wrong:

```sql
CREATE UNIQUE INDEX uq_seat_active_claim
    ON reservation_seats (seat_id) WHERE released_at IS NULL;
```

A violation of this index is an alert-worthy bug, not an expected decline. It is translated to a 409 and logged at `error`.

## Expiry

Expiry is **lazy, and that is all of it** (ADR-017): `(status = 'held' AND hold_expires_at <= now())` is an arm of the claim predicate, so a lapsed hold is claimable the instant it lapses. There is no sweeper, and nothing rewrites a lapsed hold's stored status.

Every reader therefore derives **effective status** from the expressions in `app/db/sql.py` — defined once, used by the claim predicate, the per-user count, the seat map and the gauge. A second copy that drifts breaks invariant 3.

A reserve confirms outright unless the client passes `hold_ttl_seconds`. Because a lapsed hold's `reservation_seats` row is still active, the claim closes superseded rows (`close_superseded_claims`) **after** the seat claim and **before** the insert, as its own statement — never a CTE folded into the insert (LEARN-009).

A release never resurrects a seat confirmed to someone else: cancel and sweep are themselves guarded updates predicated on the current owner and status.

## Per-user limit

Counting held seats in one statement and inserting in another loses the race. Enforcement is: lock the principal's `(user_id, show_id)` quota row, then count their active seats with the effective-status expression, then decide. The lock serializes that principal's concurrent attempts, so ten parallel requests against a limit of four end with exactly four.

The count is derived from `seats`, not from a stored counter, so it cannot drift from reality. The quota row exists to serialize, not to tally.

## Idempotency

The key row is the ownership token and the unique constraint `(user_id, key)` decides who owns it.

- Insert `in_progress` in its own committed transaction. A unique violation means someone else owns the key.
- Mark `completed` with the stored response **inside the same transaction as the reservation**, so result and key state commit atomically or not at all.
- Fingerprint the canonical request body. A matching key with a different fingerprint is 409 `IDEMPOTENCY_KEY_REUSED` — checked before any seat work.
- A completed key replays the stored body as **200** with `Idempotent-Replay: true`, never 201 (ADR-029).
- An `in_progress` key is polled for a bounded interval, then declines 409 `IDEMPOTENCY_IN_PROGRESS`.
- Only successes are stored (ADR-020). A decline or a fault rolls T2 back and releases the key, so a retry genuinely re-attempts.
- A key stuck `in_progress` past a staleness window is reclaimable, or the crash of one worker would poison a key forever. **A reclaim rotates the key's id, and T2's first statement locks the key row by id** (ADR-033): "presumed dead" is a guess, and a slow owner must find its id gone rather than commit alongside its replacement.

## What the tests must prove

Not "the endpoint returns 201". Specifically:

- N concurrent claims on one seat produce exactly one 201 and N−1 409s, zero 5xx.
- One principal firing more requests than the limit ends with exactly the limit, no more.
- The same key fired concurrently yields one reservation and identical response bodies.
- Reconciliation holds when sampled *during* the burst, not only after it.
- A cancelled or expired seat is cleanly re-bookable, and a cancel of an already-confirmed-elsewhere seat changes nothing.
- A spoofed identity field has no effect, and cross-principal cancel is refused.

A test that would still pass against a read-then-write implementation is not testing concurrency. Make it fail against a deliberately broken variant before trusting it.

## Enhancement log

- `2026-10-03` — Initial invariants: guarded conditional update, ordered multi-seat CTE, deadlock-free lock order, shared effective-status expression, quota-row serialization, idempotency key lifecycle.
- `2026-10-03` — Claim semantics verified against PostgreSQL 16.15 (LEARN-002). Rule added: the four probes above are the minimum regression set for any change to the claim predicate.
- `2026-10-03` — Pool sizing is a correctness concern, not tuning (LEARN-003): local `max_connections` is 100 and the test suite draws from the same pool, so an oversized dev pool surfaces as connection errors that look like application defects. Size against the server ceiling.
- `2026-10-04` — Brought into line with ADR-017/019/020/029, which the implementation follows: lazy-only expiry with no sweeper, superseded-row closure as a separate statement, only successes stored, replays answer 200. The skill had been teaching the superseded design.
- `2026-10-04` — A migration path is verified only when the entrypoint has run it in the built image (LEARN-013); asyncpg session guards go in `server_settings`, not a `SET`, because the pool issues `RESET ALL` on release (LEARN-014).
- `2026-10-04` — Key ownership is the row id, rotated on reclaim and locked first in T2 (ADR-033, LEARN-016). Rule earned by a real defect: recovery paths must be tested with the supposedly dead owner alive.
