# Concurrency and atomicity

This is the document every correctness claim rests on. Everything else is plumbing.

Two decisions set the shape of what follows, and both are recent: **ADR-017** makes a reserve confirm immediately unless the client opts into a hold, and makes the claim predicate's expiry arm the *entire* expiry mechanism — there is no sweeper. **ADR-019** closes superseded claim rows inside the claim transaction, which is what makes lazy-only expiry compatible with the backstop index. Read both before changing anything here.

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

This is the mechanism in its simplest form, and it is how the argument is easiest to read. **The code does not have a separate single-seat statement**: every reserve, including one seat, goes through Mechanism 2, which is the same decision with an explicit lock order.

```sql
UPDATE seats
   SET status          = CASE WHEN $ttl_seconds IS NULL THEN 'confirmed' ELSE 'held' END,
       held_by         = $user_id,
       reservation_id  = $reservation_id,
       hold_expires_at = CASE WHEN $ttl_seconds IS NULL THEN NULL
                              ELSE now() + ($ttl_seconds * INTERVAL '1 second') END,
       version         = version + 1,
       updated_at      = now(),
       request_id      = $request_id
 WHERE show_id = $show_id
   AND label   = $label
   AND (status = 'available'
        OR (status = 'held' AND hold_expires_at <= now()))
RETURNING id, label, COALESCE(price_paise, $show_price) AS price_paise;
```

**One nullable parameter decides the target state.** `$ttl_seconds IS NULL` means the client did not opt into a hold, so the claim lands directly in `confirmed` with no expiry (ADR-017); a value means a hold with that TTL. Both `status` and `hold_expires_at` are derived from the same parameter inside the statement, so the pair cannot disagree — a `confirmed` row with an expiry, or a `held` row without one, is not expressible by this statement, and `ck_seats_hold_coherent` rejects it anyway if a future path tries. Passing the status and the expiry as two independent parameters would make the incoherent combination merely *unlikely*; deriving both from one makes it unrepresentable.

The expiry arm of the `WHERE` clause is unaffected by which target state is being written: a lapsed hold is claimable whether the claimer wants a hold or an immediate confirmation.

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
- `SELECT ... FOR UPDATE SKIP LOCKED` in the claim path — skipping a momentarily locked row would decline a seat that is still genuinely available, producing spurious 409s and, with enough contention, a seat nobody can book. With the sweeper gone (ADR-017), `SKIP LOCKED` has no legitimate use anywhere in this service, and `09-repositories.md` lists it as prohibited without exception.
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
       SET status = CASE WHEN $ttl_seconds IS NULL THEN 'confirmed' ELSE 'held' END,
           held_by = $user_id, reservation_id = $reservation_id,
           hold_expires_at = CASE WHEN $ttl_seconds IS NULL THEN NULL
                                  ELSE now() + ($ttl_seconds * INTERVAL '1 second') END,
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

## Mechanism 3 — closing superseded claim rows

`uq_seat_active_claim ON reservation_seats (seat_id) WHERE released_at IS NULL` is the backstop that makes a second simultaneous active claim physically impossible. It has one collision with lazy expiry, and under ADR-017 that collision is not an edge case — it *is* the expiry path:

```
t0  A holds seat S. reservation_seats row for S is active (released_at IS NULL).
t1  A's hold lapses. No sweeper exists, so the row stays active.
t2  B claims S. The seats predicate matches the lapsed arm — B legitimately wins the seat.
t3  B inserts its own reservation_seats row for S  →  uq_seat_active_claim violation.
```

The legitimate winner gets a 409, and the backstop fires on a correct claim. **Why the index cannot simply be tightened:** a partial index predicate must be `IMMUTABLE` and `now()` is `STABLE`, so "active **and** unexpired" is not expressible as an index condition at all (LEARN-008). This is the first fix everyone proposes and PostgreSQL refuses it.

The fix is a statement in T2, after the claim has succeeded and before the new rows are inserted:

```sql
UPDATE reservation_seats SET released_at = now()
 WHERE seat_id = ANY($seat_ids::uuid[]) AND released_at IS NULL;
```

### Why it can only close a row reality has already superseded

By the time this statement runs, this transaction holds the row lock on every one of those `seats` rows, and the claim predicate has proved each was `available` or lapsed-`held`. So any `reservation_seats` row still active for one of those seat ids cannot belong to a live claim: if it did, the seat would be `confirmed` or `held` with a future expiry, the claim predicate would have excluded it, and the transaction would already have declined without reaching this statement. The seat lock is what makes that reasoning sound — nobody else can change the seat's state between the predicate and the closure.

`now()` is transaction-stable (LEARN-007), so the instant at which the predicate judged the old hold lapsed is the same instant at which this statement closes it. There is no window in which a hold is lapsed for one statement and live for the next.

### Why it is two statements, not one

Folding the closure and the insert into a single data-modifying CTE is the obvious optimisation and it is wrong. Sub-statements of one statement share a single snapshot, cannot see one another's effects, and have no defined execution order (LEARN-009) — so the insert could be evaluated before the update and violate the index anyway. Two statements, in order, in one transaction.

### Cost and ordering

The statement is served by `uq_seat_active_claim` itself: the backstop index is exactly the index this query needs, on exactly the predicate it filters by. It touches at most `per_user_limit` rows. No `ORDER BY` is required, because every row it locks belongs to a seat this transaction already holds locked — see the lock-order argument below, which is where that claim is discharged.

Rows closed increment `superseded_claims_closed_total`. That counter is the observable measure of lapsed-hold churn, and it is what replaces the sweeper's reporting. After this change there is no legitimate path that produces a `uq_seat_active_claim` violation, so the violation stays alert-worthy and stays logged at `error`.

## Lock ordering and the deadlock argument

There are exactly two kinds of transaction that lock more than one row, and each takes its locks in a fixed tier order:

| Tier | A claim (`reserve`) | A `confirm` or `cancel` |
|---|---|---|
| 0 | Its own `idempotency_keys` row, by id (ADR-033) | — |
| 1 | The `user_show_quota` row for its own principal | Its own `reservations` row |
| 2 | Seat rows, **ascending `label`** | Seat rows, **ascending `label`** |
| 3 | `reservation_seats` rows for seats already locked at tier 2 | `reservation_seats` rows for seats already locked at tier 2 |

Two invariants hold the argument together, and both are review-blocking if violated:

1. **No transaction acquires a tier-1 lock after a tier-2 lock.** A quota lock or a `reservations` lock taken after a seat lock is the deadlock.
2. **No transaction locks a `reservation_seats` row for a seat whose `seats` row it does not already hold locked.** This is what makes tier 3 free: its locks are totally covered by tier 2's, so they add no edge the tier-2 argument has not already excluded. It is also why neither Mechanism 3 nor the release statements need an `ORDER BY` on `reservation_seats`.

Deadlock requires a cycle in the wait-for graph. Every case:

- **Two claims, different principals, overlapping seats.** Tier 1 targets different rows, so no contention. At tier 2 both acquire seats in ascending label order, so if T1 holds A12 and wants A13 while T2 holds A13, T2 must already hold A12 to have reached A13 — contradiction. No cycle.
- **Two claims, same principal.** Both contend on the same quota row at tier 1, so one completes entirely before the other reaches tier 2. Strict serialization.
- **A claim versus a confirm or cancel.** Their tier-1 targets are different *tables*: a claim locks a quota row and only ever *inserts* a `reservations` row, while a confirm or cancel locks a `reservations` row and never touches a quota row. So they never contend at tier 1, and they agree at tier 2. No cycle.
- **Two confirms, or a confirm and a cancel, on one reservation.** Both contend on that single `reservations` row at tier 1. One completes, the other's guarded update then matches zero rows. Strict serialization, and the zero-row result is the decision.
- **Two confirms or cancels on different reservations with overlapping seats.** Not reachable — two reservations cannot both own a seat, which is what `uq_seat_active_claim` and the claim predicate jointly guarantee. Even if it were, tier 2 is ascending label order for both.

With the sweeper deleted (ADR-017) there is no background writer of seat state at all, so the case that previously needed `SKIP LOCKED` to stay out of the graph no longer exists.

`ORDER BY label` inside `FOR UPDATE` is what enforces tier 2, in `claim_many` **and** in the confirm and cancel statements of ADR-022. It must not be removed as a "pointless sort" — the sort is the deadlock prevention, and the earlier version of this document asserted ordered locking while specifying a plain `UPDATE ... WHERE reservation_id = $1`, which locks in scan order and left cancel outside the proof. `lock_timeout` is the backstop: a claim that waits beyond it aborts and is translated to 409 `SEAT_TAKEN` with a distinct metric label, so a lock-timeout storm is visible rather than silent. A `statement_timeout` is a different thing entirely and is a 503 — see ADR-027.

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

Statement 3's predicate is the **effective-status** notion, not the stored one: a seat whose hold has lapsed does not count against its former holder, because the next claim will hand it away. That is the same expression the counts query and the claim predicate use, imported from `db/sql.py` rather than retyped (ADR-012). Since ADR-017 a confirmed seat is the common case rather than the rare one, so the `confirmed` arm now carries most of this count's weight — it was always there, but it used to be reached only after an explicit confirm.

If `count + len(requested) > per_user_limit`, raise `PerUserLimitError` → 409 `PER_USER_LIMIT`, rollback, nothing claimed.

Two properties matter:

- The quota row lock means only one of a principal's concurrent requests is between the count and the claim at any time, so the count is never stale when acted on. Ten parallel requests against a limit of four end with exactly four.
- The count is **derived from `seats`**, not from a stored tally. A tally must be decremented on cancel, on every lapse, and on every future release path; each is an opportunity to drift, and a drifted tally either blocks a legitimate booking forever or silently raises the limit. Deriving it is self-healing by construction. The quota row exists only to serialize.

Cost: the derived count is one index-only scan on `ix_seats_by_holder`, bounded by `per_user_limit + 1` rows in practice. Cheap enough to pay per request.

Different principals never contend on step 2, so this adds no cross-user serialization — a 20k burst of distinct buyers sees 20k uncontended quota rows.

## Idempotency

### Storage

`idempotency_keys`, unique on `(user_id, key)`. The insert that wins that constraint owns the operation. Scoping the key to the user prevents one principal's key choice from colliding with another's.

`scope` (`reserve:{show_id}`) remains a column but is **not** load-bearing. It used to be the only thing distinguishing one key's use across two shows, which it could not do: it was not in the unique constraint, so the same key with the same labels against a different show produced a matching fingerprint and would have replayed the wrong show's reservation. That is now prevented by the fingerprint itself (ADR-021), and `scope` is kept only to answer "what was this key used for" during an investigation.

### Fingerprint

SHA-256 over the canonical serialization of exactly this object (ADR-021):

```
{"op": "reserve",
 "show_id": "<show id from the path>",
 "seats": [sorted, de-duplicated labels],
 "hold_ttl_seconds": <integer, omitted entirely when absent>}
```

The operation name and the show id come from the **route**, not the body, so a key cannot escape its scope by omitting a field. The idempotency key itself, the request id, and headers are excluded — the key is the lookup, not part of what is being fingerprinted.

Canonicalization: object keys sorted, whitespace normalized, seat labels sorted and de-duplicated, null-valued optional fields omitted. `{"seats":["A12","A13"]}` and `{ "seats": ["A13", "A12"] }` against the same show produce the same fingerprint because they are the same request; the same body against a different show does not, so it is a clean 409 `IDEMPOTENCY_KEY_REUSED`. Canonicalization lives in `utils/canonical_json.py`; the reserve fingerprint is built in `reservation_service` and unit-tested for the different-show, different-seats and TTL cases.

Omitting an absent `hold_ttl_seconds` rather than writing `null` matters since ADR-017: "no TTL" and "TTL 120" are different operations with different outcomes, and the fingerprint must separate them.

### Flow

```
T1: INSERT idempotency_keys (user_id, key, scope, fingerprint, state='in_progress')
    │
    ├── success → this request owns the key → proceed to T2
    │
    └── unique violation → read the existing row (own connection, released immediately)
          ├── fingerprint differs      → 409 IDEMPOTENCY_KEY_REUSED   (before any seat work)
          ├── state='completed'        → replay the stored response body as 200
          │                               header Idempotent-Replay: true
          │                               metric: declined{reason="idempotent_replay"}
          └── state='in_progress'      → poll every $interval up to $max_wait,
                │                         holding no connection between polls
                ├── becomes completed  → replay as above
                ├── row disappears     → the owner declined or faulted;
                │                         retry the insert once, then genuinely re-attempt
                └── still in_progress  → 409 IDEMPOTENCY_IN_PROGRESS with Retry-After

T2: lock the key row by id (ownership check, ADR-033) → lock quota → check limit
    → claim seats → close superseded claim rows
    → insert reservation + reservation_seats
    → UPDATE idempotency_keys SET state='completed', status_code, response_body
    → COMMIT

T2 rolls back (domain decline) → DELETE the key row, so a retry re-attempts
T2 rolls back (unexpected fault) → DELETE the key row, same path
```

### Why the key insert is its own transaction

Ownership must be visible to concurrent duplicates *immediately*. If the `in_progress` insert were inside T2, it would stay invisible until T2 commits, and two concurrent requests with the same key would both proceed to claim seats — the key would prevent nothing during exactly the window it exists for.

### Why completion is inside T2

Consider the alternative: T2 commits the reservation, then a third transaction marks the key complete. A crash between them leaves a committed reservation whose key says `in_progress` forever — the retry either blocks until the staleness window or creates a second reservation. Marking the key complete *inside* T2 makes the effect and the record of the effect atomic: both or neither.

### Only successes are stored

A completed key row always records a success. A **domain decline releases the key** (ADR-020): T2 rolls back, the key row is deleted, and a retry with the same key genuinely re-attempts.

The earlier version of this document contradicted itself on exactly this point — it said a rolled-back T2 leaves the key `in_progress`, and six lines later that declines replay as declines. There is no transaction in which a stored decline could have been written, because the decline is what rolled the transaction back.

Releasing is also the better semantic and the cheaper mechanism:

- The requirement is that a key **reserves** exactly once. A released key produces at most one reservation across any number of retries, so exactly-once holds.
- Replaying a stored decline is a lie with a shelf life. The response says "taken"; the seat may have freed thirty seconds ago. The client is denied an outcome it could now have, on the strength of a recorded answer.
- Storing a decline needs a *new* write-on-rollback transaction with its own failure story — if that write fails, the key is stuck. Releasing reuses the release path already required for the unexpected-fault case, whose failure story is already specified: a failed release leaves the key `in_progress` and the staleness reclaim handles it.

Consequence stated plainly: a key provides no shielding against a client retrying into a sold-out seat. Each retry is a real attempt. That is correct, and the rate limiter — not the key — is what bounds the cost.

Validation failures (422) are rejected before the key is claimed at all, so they never reach this path.

### The replay answers 200

Every replay answers **200** with the stored body and `Idempotent-Replay: true`, never 201 (ADR-029). A retry must not be countable as a second creation: exactly-one-201-per-hot-seat is a property the burst measures by counting status codes, and a 201 replay would make that count something other than a count of claims. `status_code` is still stored, as the record of what the original answer was, and no longer determines the replay's status. A client distinguishing "created" from "already created" reads the header.

### Stale keys

A worker that dies mid-T2, or one whose release failed, leaves an `in_progress` row with no owner. A key `in_progress` for longer than the configured staleness window is reclaimable: the reclaiming transaction takes the row `FOR UPDATE`, re-checks the age, and resets it to its own ownership. Without this, one crash poisons that key permanently.

**The key's id is the ownership token, and a reclaim rotates it (ADR-033).** "Presumed dead" is a guess from a timestamp, and a merely slow owner would otherwise proceed alongside its reclaimer: two transactions each believing they own one key, two reservations, and a `release` by the loser deleting the winner's key. So `reclaim_if_stale` sets a new `id`, and T2's first statement is `SELECT ... WHERE id = $key_id AND state = 'in_progress' FOR UPDATE`. An owner whose key was taken finds no row and answers 409 `IDEMPOTENCY_IN_PROGRESS` before any seat work; an owner that got there first holds the row lock, so a reclaimer waits, re-checks against the committed row, and replays instead. The key row is locked before the quota row and nothing else locks it inside a multi-statement transaction, so it adds no edge to the deadlock argument.

The reclaim is **lazy**, triggered by the arrival of a duplicate rather than by a worker — the same shape as seat expiry, and for the same reason. Nothing scans for stale keys, so `ix_idem_stale` has no reader and is dropped (ADR-017).

### A waiter holds no connection between polls

The bounded poll is a client-facing latency budget, not a database-side one. A waiter acquires a connection, performs one indexed point read on `uq_idem_user_key`, **releases the connection, and only then sleeps** (ADR-026). The service acquires a connection for each poll and releases it before the sleep; `idempotency_repo.get` is called on a connection that is not in a transaction.

Holding the connection across the wait would be a self-inflicted outage: a few hundred concurrent duplicates would occupy the pool for the whole wait budget, exhaust it, and produce exactly the 503s ADR-016 forbids. The cost of the fix is round trips — at the current defaults, up to 40 point reads for a waiter that times out — which is the cheap resource. Connection-seconds are the scarce one.

### Retention

Keys carry `expires_at`. With the sweeper gone there is no in-process loop to purge them: retention is a scheduled maintenance query, documented in the `13-deployment.md` runbook and tracked as RISK-007. No repository method or schedule for it exists yet. Growth is one row per reserve that won its key — trivial over a burst, material over months, which is the timescale on which this is maintenance rather than machinery.

## Holds and expiry

Since ADR-017 a hold is **opt-in**. A reserve with no `hold_ttl_seconds` lands in `confirmed` with no expiry; a reserve with one lands in `held` with `hold_expires_at`, and the client must confirm before it lapses. All three seat states remain genuinely reachable, which is what the `GET /shows/{id}` contract requires — the contract is honoured by behaviour, not by a status nothing produces.

**Expiry is lazy, and that is all of it.** `(status='held' AND hold_expires_at <= now())` is an arm of the claim's `WHERE` clause, so a lapsed hold is claimable the instant it lapses, with no worker involved. LEARN-002's fourth probe verified this directly against PostgreSQL 16.15 before any application code existed.

There is **no sweeper**. `workers/hold_sweeper.py` is not part of this design. Its only job was making stored state match reality, and every reader that matters derives effective status instead — which is strictly better, because a derived read is exact at the instant of the read rather than exact to within one sweep interval. What the sweeper cost was a second writer of seat state, with its own lock story and its own failure mode, sitting next to the one path in the service that must not be gotten wrong.

### Two effective-status expressions, each defined once

Because stored status is not the authority for a lapsed hold, both tables need a derived view of state, and both are defined once in `db/sql.py` and imported everywhere (ADR-012, extended by ADR-017):

| Expression | Over | Consumed by |
|---|---|---|
| Seat effective status | `seats` | the claim predicate, the per-user-limit count, the `GET /shows/{id}` counts and seat rows, the per-show gauges |
| Reservation effective status | `reservations` | `GET /reservations`, `GET /reservations/{id}`, and the confirm/cancel diagnostic read |

Seat: a lapsed hold reports `available`, matching what a claim would see. If the claim predicate and the counts query disagreed about that, the API would show `held` for a seat the next claim hands out, and reconciliation during a burst would see counts that contradict behaviour.

Reservation: a `held` row whose `hold_expires_at` has passed reports **`expired`**. This is the answer to "what does a lapsed hold read as, now that nothing rewrites it" — it reads as expired, everywhere, because every reader derives it. The stored value is corrected only when the owner confirms or cancels, or never. A reader that reports the stored `held` is a defect of the same class as a second copy of the seat expression.

Divergent copies of either expression remain the single most likely way this design fails a reconciliation check.

### Release never resurrects

Confirm and cancel are specified in full by **ADR-022**. The shape, and why it is this shape:

**The decision is a guarded `UPDATE` on the `reservations` row**, which decides and serializes in one statement:

```sql
-- confirm
UPDATE reservations
   SET status='confirmed', confirmed_at=now(), hold_expires_at=NULL,
       updated_at=now(), request_id=$request_id
 WHERE id = $reservation_id AND user_id = $user_id
   AND status = 'held' AND hold_expires_at > now()
RETURNING id, show_id, seat_count, amount_paise, currency, confirmed_at;

-- cancel
UPDATE reservations
   SET status='cancelled', cancelled_at=now(), hold_expires_at=NULL,
       updated_at=now(), request_id=$request_id
 WHERE id = $reservation_id AND user_id = $user_id
   AND status = 'held' AND hold_expires_at > now()
RETURNING id, show_id, seat_count, cancelled_at;
```

Three things come out of making the `reservations` row the decision point: the row lock serializes that principal's concurrent confirms and cancels, `user_id` in the `WHERE` clause makes ownership a predicate rather than a fetch-then-compare, and the returned row count is the decision with no window between check and effect — the same shape as the claim.

**The effect is the ordered CTE**, guarded on current ownership *and* on the unexpired arm:

```sql
-- cancel: release the seats this reservation still owns
WITH owned AS (
    SELECT id, label FROM seats
     WHERE reservation_id = $reservation_id
       AND status = 'held' AND hold_expires_at > now()
     ORDER BY label
       FOR UPDATE
),
released AS (
    UPDATE seats s
       SET status='available', held_by=NULL, reservation_id=NULL, hold_expires_at=NULL,
           version=version+1, updated_at=now(), request_id=$request_id
      FROM owned o WHERE s.id = o.id
    RETURNING s.label
)
SELECT label FROM released ORDER BY label;
```

```sql
-- confirm: promote the seats this reservation still owns
WITH owned AS (
    SELECT id, label FROM seats
     WHERE reservation_id = $reservation_id
       AND status = 'held' AND hold_expires_at > now()
     ORDER BY label
       FOR UPDATE
),
promoted AS (
    UPDATE seats s
       SET status='confirmed', hold_expires_at=NULL,
           version=version+1, updated_at=now(), request_id=$request_id
      FROM owned o WHERE s.id = o.id
    RETURNING s.label
)
SELECT label FROM promoted ORDER BY label;
```

Cancel then closes the reservation's `reservation_seats` rows with `released_at`. Confirm leaves them active — the claim is still live, it has simply been paid for.

Four properties, each load-bearing:

- **`ORDER BY label` inside `FOR UPDATE`** puts both statements inside the deadlock argument. The previous specification used a plain `UPDATE seats ... WHERE reservation_id = $1`, which locks in scan order; a cancel of a multi-seat reservation racing a claim on the same seats could cycle, and the proof did not cover the SQL as written.
- **The unexpired arm** is what makes expiry mean something on these paths. Without it, a lapsed-but-unrewritten hold is still promotable — and with no sweeper there is nothing to rewrite it, so "confirm a hold three hours after it expired" would have succeeded. `05-auth-and-rbac.md`'s hold-hijack threat relied on `reservation_id` having moved on, which is true only for a seat that was *re-claimed*, not for one merely lapsed.
- **A seat that has moved on does not match.** Re-claimed by someone else, so `reservation_id` now points elsewhere: zero rows, so the release cannot take a seat from its new owner and the confirm cannot steal it back.
- **A shortfall rolls back.** Fewer rows than `seat_count` means some seat is no longer this reservation's, so the transaction aborts and the request answers 409 `SEAT_TAKEN`. The shortfall is believed unreachable — `now()` is transaction-stable (LEARN-007), so a reservation that is unexpired at that instant cannot own a seat that is expired at the same instant — but it is asserted rather than assumed, because the argument depends on that semantic.

**Zero rows from the decision statement** means the service must still say *why*. It re-reads the reservation owner-scoped, using the reservation effective-status expression, purely to choose the code: `cancelled` → 200 for a repeat cancel or 409 `RESERVATION_CANCELLED` for a confirm; `confirmed` → 200 for a repeat confirm or 409 `RESERVATION_CONFIRMED` for a cancel; effectively `expired` → 409 `RESERVATION_EXPIRED`; absent or not owned → 404 `RESERVATION_NOT_FOUND`. That read is outside the lock and may observe a later state than the one that caused the decline. It is diagnosis, not control: the decision was already made and committed to, and every state the read can report is a state the reservation genuinely held.

Cancel of a lapsed hold is therefore 409 `RESERVATION_EXPIRED`, not a silent successful release. Nothing is leaked by refusing: the seats are already effectively available, and their stale `reservation_seats` rows are closed by the next claim under Mechanism 3.

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
| Clock skew between app and database | All expiry arithmetic uses the database's `now()`. The application never computes an expiry boundary itself. `now()` is also transaction-stable, so a transaction's own statements cannot disagree about whether a hold has lapsed (LEARN-007). |
| Long-running transaction holding seat locks | `statement_timeout`, `lock_timeout`, and `idle_in_transaction_session_timeout` bound every path. The first two are deliberately ordered — `lock_timeout` fires first and declines 409; a `statement_timeout` is a fault and answers 503 (ADR-027). |
| Lazy-only expiry leaving stale claim rows | A lapsed hold's `reservation_seats` row stays active until the seat is next claimed, when Mechanism 3 closes it in the same transaction. A raw "count rows where `released_at IS NULL`" therefore overcounts between lapse and re-claim. Every reader that matters derives effective status instead; the one that cannot — the backstop index — is handled by Mechanism 3. RISK-007. |
| An idempotency waiter occupying the pool | A waiter releases its connection between polls (ADR-026), so a wait budget costs round trips rather than connection-seconds. |
| Connection pool exhaustion under burst | Pool size is capped below the database's connection ceiling and requests queue at the pool. A queue-wait timeout returns 503 with a code — the one legitimate 5xx, excluded from the claim path by sizing. See [11-scalability.md](11-scalability.md). |
| Two app instances | The decision is entirely in the database, so N instances behave identically to one. Nothing in the claim path holds in-process state. Only rate limiting is per-instance, documented in [07-middleware.md](07-middleware.md). |
| `READ COMMITTED` re-evaluation being an implementation detail | It is documented PostgreSQL behaviour for `UPDATE` and `FOR UPDATE`, not an accident. It is also not relied on alone — `uq_seat_active_claim` makes a double active claim impossible independently of it. |
| Replica lag showing a stale seat state | No replica. Reads go to the primary. |
| A future general-admission feature | A capacity counter is a different mechanism (a guarded decrement on a counter row) and would need its own argument. Out of scope; noted so it is not bolted onto this path. |
