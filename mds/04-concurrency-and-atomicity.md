# Concurrency and atomicity

This is the document the service is judged by. Everything else is plumbing.

## The problem

```
t0  A: SELECT status FROM seats WHERE label='A12'  → 'available'
t1  B: SELECT status FROM seats WHERE label='A12'  → 'available'
t2  A: UPDATE seats SET status='held', held_by=A   → 1 row
t3  B: UPDATE seats SET status='held', held_by=B   → 1 row
```

Two holders of A12. The gap between the read and the write is the defect, and no amount of application-level checking closes it. Wrapping both statements in a transaction at `READ COMMITTED` does not close it either: both `UPDATE`s succeed, the second overwriting the first — a classic lost update.

The fix is not to check more carefully. It is to make the decision and the effect the same operation.

## Mechanism 1 — guarded conditional UPDATE (single seat)

```sql
UPDATE seats
   SET status          = 'held',
       held_by         = $user_id,
       reservation_id  = $reservation_id,
       hold_expires_at = now() + ($ttl_seconds * INTERVAL '1 second'),
       version         = version + 1,
       updated_at      = now(),
       request_id      = $request_id
 WHERE show_id = $show_id
   AND label   = $label
   AND (status = 'available'
        OR (status = 'held' AND hold_expires_at <= now()))
RETURNING id, label, COALESCE(price_paise, $show_price) AS price_paise;
```

The returned row count **is** the decision:

- 1 row → this transaction owns the seat.
- 0 rows → the seat was already active for someone else. Clean 409 `SEAT_TAKEN`.

### Why it is race-free

Under `READ COMMITTED`, when transaction B's `UPDATE` reaches a row that transaction A has already updated but not committed, B blocks on A's row lock. When A commits, B does not proceed with the row version it originally matched — PostgreSQL re-reads the now-current row and **re-evaluates the `WHERE` clause against it** (the EvalPlanQual path). A's commit has set `status='held'` with a future `hold_expires_at`, so B's predicate is false, the row is excluded, and B's `UPDATE` reports zero rows affected.

Three properties follow:

1. **No gap.** There is no instant between evaluating the condition and applying the effect, because they are one statement under one row lock.
2. **No lost update.** B cannot overwrite A's claim, because B's predicate no longer matches after A commits.
3. **Exactly one winner.** Of N contenders on one row, the row lock admits them one at a time and the predicate is true for exactly the first. 500 requests for A12 → one 201, 499 × 409.

If A rolls back instead of committing, B's re-evaluation sees `available` and B wins. Correct: a rolled-back claim never happened.

### What this rules out

- `SELECT` then `UPDATE` — forbidden in the claim path, in any wrapper.
- `SELECT ... FOR UPDATE SKIP LOCKED` in the claim path — skipping a momentarily locked row would decline a seat that is still genuinely available, producing spurious 409s and, with enough contention, a seat nobody can book. `SKIP LOCKED` is correct only in the sweeper.
- Advisory locks keyed on seat labels — works, but it is a second locking system to reason about alongside the row locks that already exist, with no benefit.
- `SERIALIZABLE` isolation — correct, but it converts contention into serialization failures that must be retried, which under a 20k burst means a retry storm. `READ COMMITTED` with a guarded update turns contention into a *decision* instead of a *conflict*.

## Mechanism 2 — ordered CTE (multi-seat, all-or-nothing)

```sql
WITH candidate AS (
    SELECT id, label, COALESCE(price_paise, $show_price) AS price_paise
      FROM seats
     WHERE show_id = $show_id
       AND label   = ANY($labels::text[])
       AND (status = 'available'
            OR (status = 'held' AND hold_expires_at <= now()))
     ORDER BY label
       FOR UPDATE
),
claimed AS (
    UPDATE seats s
       SET status = 'held', held_by = $user_id, reservation_id = $reservation_id,
           hold_expires_at = now() + ($ttl_seconds * INTERVAL '1 second'),
           version = version + 1, updated_at = now(), request_id = $request_id
      FROM candidate c
     WHERE s.id = c.id
    RETURNING s.id, s.label, c.price_paise
)
SELECT * FROM claimed;
```

The service compares the returned row count to the number of labels requested:

- Equal → every seat was claimed. Proceed to insert the reservation.
- Fewer → raise `SeatTakenError(conflicts=requested − claimed)`. The transaction rolls back, so **not one** of the successfully claimed seats remains held.

All-or-nothing is a property of the transaction, not of application cleanup. There is no compensating release to get wrong, and no window in which a partially claimed request is visible to anyone.

`FOR UPDATE` here performs the same `READ COMMITTED` re-evaluation as the single-seat `UPDATE`: a row another transaction has just claimed fails the predicate on re-check and drops out of `candidate`, so the count falls short and the request declines.

## Lock ordering and the deadlock argument

Every claiming transaction takes locks in exactly this order:

1. The `user_show_quota` row for `(its own principal, show)`.
2. Seat rows for the requested labels, in **ascending `label`** order.

Deadlock requires a cycle in the wait-for graph. Consider the cases:

- **Different principals, overlapping seats.** Step 1 targets different rows, so no contention there. In step 2 both acquire seats in ascending label order, so if T1 holds A12 and wants A13 while T2 holds A13, T2 must already hold A12 to have reached A13 — contradiction. No cycle.
- **Same principal, concurrent requests.** Both contend on the same quota row at step 1, so one completes entirely before the other begins step 2. Strict serialization, no cycle.
- **A claim versus the sweeper.** The sweeper takes seat locks with `SKIP LOCKED` and never touches a quota row, so it never waits on a claimer and cannot participate in a cycle.
- **A claim versus a cancel or confirm.** Both are single-row guarded updates on seats belonging to one reservation, acquired in ascending label order, and neither takes a quota lock after a seat lock.

The invariant that makes all of this hold: **no transaction ever acquires a quota lock after acquiring a seat lock.** Any code that does becomes the deadlock, and it is the first thing to check when reviewing a new path.

`ORDER BY label` inside `FOR UPDATE` is what enforces step 2. It must not be removed as a "pointless sort" — the sort is the deadlock prevention. `lock_timeout` is the backstop: a claim that waits beyond it aborts and is translated to 409 `SEAT_TAKEN` with a distinct metric label, so a lock-timeout storm is visible rather than silent.

### Hot-seat queue depth

500 contenders on one row form a 500-deep lock queue. Each holder's critical section is a sub-millisecond index update, so the queue drains fast, and `lock_timeout` bounds the tail. The alternative — `NOWAIT`, declining immediately on any lock contention — removes the queue but declines seats that were merely momentarily locked by a transaction that then rolled back, producing spurious 409s. Blocking with a timeout is the better trade: no false declines, bounded latency. Recorded as ADR-004.

## Per-user limit under concurrency

Counting and then inserting loses the race in exactly the way the double-sell does:

```
A: count held for user U → 3   (limit 4)
B: count held for user U → 3
A: claim 1 → U now holds 4
B: claim 1 → U now holds 5      ✗
```

Enforcement, inside the same transaction, before any seat is touched:

```sql
-- 1. ensure the lock target exists (no-op if it does)
INSERT INTO user_show_quota (user_id, show_id) VALUES ($user_id, $show_id)
ON CONFLICT DO NOTHING;

-- 2. serialize this principal's concurrent attempts for this show
SELECT 1 FROM user_show_quota
 WHERE user_id = $user_id AND show_id = $show_id
   FOR UPDATE;

-- 3. count what they actually hold, from the seats themselves
SELECT count(*) FROM seats
 WHERE show_id = $show_id AND held_by = $user_id
   AND (status = 'confirmed' OR (status = 'held' AND hold_expires_at > now()));
```

If `count + len(requested) > per_user_limit`, raise `PerUserLimitError` → 409 `PER_USER_LIMIT`, rollback, nothing claimed.

Two properties matter:

- The quota row lock means only one of a principal's concurrent requests is between the count and the claim at any time, so the count is never stale when acted on. Ten parallel requests against a limit of four end with exactly four.
- The count is **derived from `seats`**, not from a stored tally. A tally must be decremented on cancel, on expiry sweep, and on every future release path; each is an opportunity to drift, and a drifted tally either blocks a legitimate booking forever or silently raises the limit. Deriving it is self-healing by construction. The quota row exists only to serialize.

Cost: the derived count is one index-only scan on `ix_seats_by_holder`, bounded by `per_user_limit + 1` rows in practice. Cheap enough to pay per request.

Different principals never contend on step 2, so this adds no cross-user serialization — a 20k burst of distinct buyers sees 20k uncontended quota rows.

## Idempotency

### Storage

`idempotency_keys`, unique on `(user_id, key)`. The insert that wins that constraint owns the operation. Scoping the key to the user prevents one principal's key choice from colliding with another's; `scope` (`reserve:{show_id}`) prevents a key reused across operations from replaying the wrong response.

### Fingerprint

SHA-256 over a canonical serialization of the request body: keys sorted, whitespace normalized, seat labels sorted and de-duplicated, null-valued optional fields omitted. `{"seats":["A12","A13"]}` and `{ "seats": ["A13", "A12"] }` produce the same fingerprint because they are the same request. Canonicalization lives in `utils/canonical_json.py` and is unit-tested against reordering, whitespace, and casing.

### Flow

```
T1: INSERT idempotency_keys (user_id, key, scope, fingerprint, state='in_progress')
    │
    ├── success → this request owns the key → proceed to T2
    │
    └── unique violation → SELECT the existing row
          ├── fingerprint differs      → 409 IDEMPOTENCY_KEY_REUSED   (before any seat work)
          ├── state='completed'        → replay stored status_code + response_body
          │                               header Idempotent-Replay: true
          │                               metric: declined{reason="idempotent_replay"}
          └── state='in_progress'      → poll the row every $interval up to $max_wait
                ├── becomes completed  → replay as above
                ├── row disappears     → the owner failed; retry the insert once
                └── still in_progress  → 409 IDEMPOTENCY_IN_PROGRESS with Retry-After

T2: lock quota → check limit → claim seats → insert reservation + reservation_seats
    → UPDATE idempotency_keys SET state='completed', status_code, response_body
    → COMMIT
```

### Why the key insert is its own transaction

Ownership must be visible to concurrent duplicates *immediately*. If the `in_progress` insert were inside T2, it would stay invisible until T2 commits, and two concurrent requests with the same key would both proceed to claim seats — the key would prevent nothing during exactly the window it exists for.

### Why completion is inside T2

Consider the alternative: T2 commits the reservation, then a third transaction marks the key complete. A crash between them leaves a committed reservation whose key says `in_progress` forever — the retry either blocks until the staleness window or creates a second reservation. Marking the key complete *inside* T2 makes the effect and the record of the effect atomic: both or neither.

If T2 rolls back, the key remains `in_progress` with nothing claimed, and the staleness path below reclaims it.

### Stale keys

A worker that dies mid-T2 leaves an `in_progress` row with no owner. A key `in_progress` for longer than the configured staleness window is reclaimable: the reclaiming transaction takes the row `FOR UPDATE`, re-checks the age, and resets it to its own ownership. Without this, one crash poisons that key permanently.

### Replaying declines

A domain decline is stored and replayed. `POST /reserve` with key K returning 409 `SEAT_TAKEN`, retried with K, returns the same 409 — that is what exactly-once means for a request whose outcome was a decline. An unexpected 5xx is different: the outcome is unknown, so the key row is deleted and the client may genuinely re-attempt. Validation failures (422) are rejected before the key is claimed at all.

### Retention

Keys carry `expires_at` and are purged by the sweeper after a window comfortably longer than any plausible client retry. Keeping them forever grows an index on the hot write path for no benefit.

## Holds and expiry

A hold is `seats.status='held'` plus `hold_expires_at`. Expiry is enforced in two independent places, and the two must agree:

**Lazily, in the claim predicate.** `(status='held' AND hold_expires_at <= now())` is an arm of the claim's `WHERE` clause, so a lapsed hold is claimable the instant it lapses. This is the arm that matters for correctness: a seat is never unbookable because a background worker is behind, and expiry works correctly even with the sweeper dead.

**Eventually, by the sweeper.** Every `$interval`, in bounded batches:

```sql
WITH lapsed AS (
    SELECT id FROM seats
     WHERE status = 'held' AND hold_expires_at <= now()
     ORDER BY hold_expires_at
     LIMIT $batch
       FOR UPDATE SKIP LOCKED
)
UPDATE seats s
   SET status='available', held_by=NULL, reservation_id=NULL,
       hold_expires_at=NULL, version=version+1, updated_at=now()
  FROM lapsed l WHERE s.id = l.id
RETURNING s.id, s.reservation_id;
```

then marks the affected reservations `expired` and closes their `reservation_seats` rows with `released_at`.

`SKIP LOCKED` is correct here and nowhere else: a row another worker or a claimer is already handling will be dealt with by them, and skipping it costs only a cycle of latency. The sweeper's job is to keep *reported* state honest; the claim predicate already keeps *actual* state correct.

### The effective-status expression

Because a lapsed-but-unswept hold is claimable, it must report as `available` in `GET /shows/{id}` too, or the API would show `held` for a seat the next claim will hand out — and reconciliation checks during a burst would see counts that disagree with behaviour. The expression is defined **once** in `db/sql.py` and used by the claim predicate, the sweeper, and the counts query. Three divergent copies is the single most likely cause of a reconciliation failure in this design.

### Release never resurrects

Cancel and confirm are guarded updates predicated on current ownership:

```sql
UPDATE seats SET status='available', held_by=NULL, reservation_id=NULL, hold_expires_at=NULL
 WHERE reservation_id = $reservation_id AND status = 'held';
```

A seat that has moved on — swept and re-claimed by someone else, so `reservation_id` now points elsewhere — does not match, so the release affects zero rows and cannot take a seat away from its new owner. Confirm is the mirror image, predicated on `status='held' AND reservation_id = $id`, so confirming a hold that lapsed and was re-claimed fails cleanly with 409 rather than stealing a confirmed seat.

## Reconciliation

`available + held + confirmed == total_seats` holds for a structural reason, not an enforced one:

- `seats` has exactly `total_seats` rows per show, inserted once in the show-creation transaction, never inserted or deleted again.
- `status` is a single `CHECK`-constrained column with exactly those three values.
- The counts query derives all three from one scan of one snapshot, so no concurrent write can land between two separate counts.
- `ck_seats_hold_coherent` makes a half-written transition unrepresentable, so a bug in a transition raises a constraint violation instead of producing an uncountable row.

The invariant therefore cannot be broken by concurrency. It can only be broken by code that adds or removes seat rows after creation, or that introduces a fourth state. Both are explicitly forbidden.

## Threats to the argument

Documented so a reviewer checks them rather than rediscovering them.

| Threat | Why it does not apply, or what bounds it |
|---|---|
| Clock skew between app and database | All expiry arithmetic uses the database's `now()`. The application never computes an expiry boundary itself. |
| Long-running transaction holding seat locks | `statement_timeout`, `lock_timeout`, and `idle_in_transaction_session_timeout` bound every path. |
| Connection pool exhaustion under burst | Pool size is capped below the database's connection ceiling and requests queue at the pool. A queue-wait timeout returns 503 with a code — the one legitimate 5xx, excluded from the claim path by sizing. See [11-scalability.md](11-scalability.md). |
| Two app instances | The decision is entirely in the database, so N instances behave identically to one. Nothing in the claim path holds in-process state. Only rate limiting is per-instance, documented in [07-middleware.md](07-middleware.md). |
| `READ COMMITTED` re-evaluation being an implementation detail | It is documented PostgreSQL behaviour for `UPDATE` and `FOR UPDATE`, not an accident. It is also not relied on alone — `uq_seat_active_claim` makes a double active claim impossible independently of it. |
| Replica lag showing a stale seat state | No replica. Reads go to the primary. |
| A future general-admission feature | A capacity counter is a different mechanism (a guarded decrement on a counter row) and would need its own argument. Out of scope; noted so it is not bolted onto this path. |
