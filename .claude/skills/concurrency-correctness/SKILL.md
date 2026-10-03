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

`SKIP LOCKED` must **not** appear in the claim path — skipping a momentarily locked row would decline a seat that is still available. It is correct only in the sweeper, which may safely ignore rows another worker is already handling.

`lock_timeout` is set per claiming transaction. A timeout is translated to a 409 with its own metric label, never a 500 and never an indefinite wait.

## Belt and braces

The guarded update is the mechanism. A partial unique index is the backstop that makes a second active claim physically impossible even if the predicate were ever wrong:

```sql
CREATE UNIQUE INDEX uq_seat_active_claim
    ON reservation_seats (seat_id) WHERE released_at IS NULL;
```

A violation of this index is an alert-worthy bug, not an expected decline. It is translated to a 409 and logged at `error`.

## Expiry

Expiry is enforced in two places and the two must agree:

- **Lazily**, by the `(status = 'held' AND hold_expires_at <= now())` arm of the claim predicate, so an expired hold is claimable the instant it lapses with no worker involved.
- **Eventually**, by the sweeper, which returns lapsed seats to `available` and marks their reservations `expired` so state reads stay honest.

The **effective status** expression — treating a lapsed hold as available — must be defined once and reused by the claim predicate, the sweeper, and the `GET /shows/{id}` counts query. Three copies that drift break invariant 3. One definition, imported everywhere.

A release never resurrects a seat confirmed to someone else: cancel and sweep are themselves guarded updates predicated on the current owner and status.

## Per-user limit

Counting held seats in one statement and inserting in another loses the race. Enforcement is: lock the principal's `(user_id, show_id)` quota row, then count their active seats with the effective-status expression, then decide. The lock serializes that principal's concurrent attempts, so ten parallel requests against a limit of four end with exactly four.

The count is derived from `seats`, not from a stored counter, so it cannot drift from reality. The quota row exists to serialize, not to tally.

## Idempotency

The key row is the ownership token and the unique constraint `(user_id, key)` decides who owns it.

- Insert `in_progress` in its own committed transaction. A unique violation means someone else owns the key.
- Mark `completed` with the stored response **inside the same transaction as the reservation**, so result and key state commit atomically or not at all.
- Fingerprint the canonical request body. A matching key with a different fingerprint is 409 `IDEMPOTENCY_KEY_REUSED` — checked before any seat work.
- A completed key replays the stored status and body, flagged as a replay.
- An `in_progress` key is polled for a bounded interval, then declines 409 `IDEMPOTENCY_IN_PROGRESS`.
- Domain declines are stored and replayed; they are part of exactly-once. An unexpected 5xx releases the key so a retry can genuinely re-attempt.
- A key stuck `in_progress` past a staleness window is reclaimable, or the crash of one worker would poison a key forever.

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
