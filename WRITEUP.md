# Write-up

## The problem, and the one decision that solves it

Two requests read a seat as available; both write; the seat has two owners. No amount
of checking in the application closes that gap, and wrapping the read and the write in
a `READ COMMITTED` transaction does not either — the second write simply overwrites
the first.

The fix is to make the decision and the effect **one statement**. A seat is one row,
and the claim is a conditional update of that row
([app/repositories/seat_repo.py](app/repositories/seat_repo.py)):

```sql
WITH candidate AS (
    SELECT id, label, COALESCE(price_paise, $show_price) AS price_paise
      FROM seats
     WHERE show_id = $show AND label = ANY($labels)
       AND (status = 'available' OR (status = 'held' AND hold_expires_at <= now()))
     ORDER BY label
       FOR UPDATE
)
UPDATE seats s SET status = ..., held_by = $user, reservation_id = $reservation, ...
  FROM candidate c WHERE s.id = c.id
RETURNING s.id, s.label, c.price_paise;
```

**The number of rows returned is the decision.** All of them: this transaction owns
the seats. Fewer: someone else holds one, the transaction rolls back, and the caller
gets `409 SEAT_TAKEN` naming the conflicts.

### Why it is race-free

Under `READ COMMITTED`, when B's statement reaches a row A has updated but not yet
committed, B blocks on A's row lock. When A commits, PostgreSQL does not let B proceed
with the row version it first saw: it re-reads the committed row and **re-evaluates
the `WHERE` clause against it**. A's commit made the seat `confirmed`, B's predicate
is now false, the row drops out, and B's row count comes up short.

So there is no instant between check and write (they are the same statement under the
same lock), no lost update (the loser's predicate no longer matches), and exactly one
winner (the lock admits contenders one at a time and the predicate is true for the
first only). If A rolls back instead, B's re-check sees `available` and B wins — a
rolled-back claim never happened.

A loser is a **zero-row result, not an exception**. That is what makes "no 5xx for a
domain outcome" a property of the mechanism rather than of error handling. This was
probed directly against PostgreSQL before any application code existed, and is
re-proven on every test run.

A partial unique index, `uq_seat_active_claim ON reservation_seats (seat_id) WHERE
released_at IS NULL`, is the backstop: a second live claim on one seat is physically
unrepresentable even if the predicate were ever wrong. If it fires, the client still
gets a `409`, and the log line is an alert.

All-or-nothing is a property of the transaction, not of cleanup code. There is no
compensating release to get wrong.

## Deadlock avoidance

Every claiming transaction takes locks in one fixed order:

1. its own principal's quota row,
2. seat rows in **ascending label order**.

`ORDER BY label` under `FOR UPDATE` is not a sort for presentation — it is the
deadlock prevention. Two buyers asking for `[A1, A2]` and `[A2, A1]` both lock A1
first, so neither can hold what the other is waiting for; no cycle is constructible.
Different principals never contend at step 1; the same principal serializes there.
Cancel and confirm lock their own reservation row first, then seats in the same order.

`lock_timeout` (2s) bounds the wait and is translated to `409`; it is configured
strictly below `statement_timeout`, validated at boot, so contention can never surface
as a statement cancellation. `SKIP LOCKED` and `NOWAIT` are deliberately absent: both
would decline a seat that is merely momentarily locked by a transaction about to roll
back.

## Per-user limit

Count-then-insert loses the same race the double-sell does. So a claim first locks the
principal's `(user, show)` quota row — serializing only *that user's* concurrent
requests — then counts what they hold. The count is **derived from the seats**, never
stored: a stored tally must be decremented on every cancel and every lapse, and each
is a chance to drift. The quota row is a lock target, not a counter.

## Idempotency

The key row is an ownership token; `UNIQUE (user_id, key)` decides who owns it.

- **T1** inserts the key as `in_progress` and commits at once — ownership must be
  visible to a concurrent duplicate *immediately*, or the key protects nothing during
  exactly the window it exists for.
- **T2** is the claim, and it marks the key `completed` with the response body
  **inside the same transaction as the reservation**. A third transaction would let a
  crash leave a committed reservation whose key says `in_progress` forever.
- A duplicate that loses T1 reads the row: a different fingerprint is
  `409 IDEMPOTENCY_KEY_REUSED`; a completed row is replayed; an in-progress row is
  polled for a bounded time, **releasing its connection between polls** — holding it
  would let a few hundred retries exhaust the pool.
- The fingerprint covers the operation, the show id *from the path*, the sorted labels
  and the TTL, so a key cannot be replayed against another show.
- **A replay is `200`, never `201`**, so retries cannot be miscounted as creations.
- **Only successes are stored.** A decline rolls T2 back and releases the key: a
  stored "taken" is a lie with a shelf life, and a retry should be a real attempt.
- A key stuck `in_progress` past a staleness window (its owner died) is reclaimable by
  one guarded `UPDATE`, so a crash cannot poison a key. Taking a key over **rotates
  its id**, and the claim's first statement locks the key row by id — so an owner that
  was only slow, not dead, finds its id gone and stops before touching a seat. A test
  written with the staleness window at zero found the original version let both the
  old and the new owner proceed.

## Holds and expiry

A reserve confirms outright by default; a hold is opt-in via `hold_ttl_seconds`. That
keeps the path load actually exercises free of any expiry edge.

**Both release models are built.** The owner can cancel a confirmed booking or a
live hold: one guarded `UPDATE` on the reservation row decides it, then the seats are
released by an ordered `FOR UPDATE` guarded on `reservation_id` — so a cancel can only
ever release seats that reservation still owns. A seat that lapsed and was claimed by
someone else carries their reservation id and cannot match; a repeat cancel is a
no-op. The first version only cancelled holds, which against a service that confirms
by default meant cancel refused the normal case; reading the brief again caught it.

Expiry is **lazy, and that is all of it**: `status = 'held' AND hold_expires_at <=
now()` is an arm of the claim predicate, so a lapsed hold is claimable the instant it
lapses. There is no sweeper — its only job would be making stored state match
reality, and every reader derives effective status instead, from expressions defined
exactly once ([app/db/sql.py](app/db/sql.py)) and shared by the claim, the limit
count, the seat map and the gauge. A second writer of seat state, with its own lock
story, next to the one path that must not be wrong, was not worth having.

Two PostgreSQL facts shaped this. A partial index predicate must be `IMMUTABLE`, so
the backstop index cannot say "active *and unexpired*" — a lapsed hold's claim row
would collide with the legitimate next claim. So the claim closes superseded rows
first, as a **separate statement**: folded into the insert as a CTE, the two would
share one snapshot with no defined order. And `now()` is transaction-stable, so a hold
cannot be lapsed for one statement and live for the next.

## Reconciliation

`available + held + confirmed == total` holds structurally, not by enforcement: seat
rows are created once with the show and never added or removed; status is one
`CHECK`-constrained column; a coherence constraint makes a half-written transition
unrepresentable; and counts are derived from a single statement's snapshot. The burst
samples this *during* the stampede, not only after.

## Consistency under partition: CP

PostgreSQL is the only authority for seat state, and nothing in the claim path holds
in-process state — N instances behave like one. If the service cannot reach the
database it does not sell: `/readyz` fails closed and requests get `503
DATABASE_UNAVAILABLE`. It never answers from a cache and never queues a claim to apply
later. An unavailable box office is recoverable; a double-sold seat is not. The
ambiguous case — a client that times out without learning the outcome — is what the
idempotency key resolves: the retry returns the original reservation or makes the one
real attempt.

## What would page at 2am

| Signal | Why |
|---|---|
| `unhandled_exceptions_total` > 0, or any 5xx on reserve | A decline surfaced as a fault. Violates a hard requirement |
| `claim_backstop_violated` in the logs | The unique index caught a second live claim: the predicate has a bug |
| `/readyz` failing | Database unreachable; nothing can be sold |
| `claim_deadlock` in the logs | The lock order was broken by a change |
| `reservations_declined_total{reason="lock_timeout"}` climbing | Hot-seat queues are exceeding the lock timeout |
| `audit_records_dropped_total` climbing | The audit buffer is full: the writer is stalled or the database is slow. Bookings are unaffected by design, which is why this needs an alert of its own |
| `rate_limited_total` climbing on `reserve` or `read` | Real users are being throttled: a ceiling is too low, or the proxy-hop count is wrong and clients share a bucket |

Deliberately **not** paging: a high rate of `seat_taken`, `per_user_limit` or
`idempotent_replay`. That is the service working during an on-sale, and an alert that
fires whenever the product succeeds gets muted.

## Evidence

- **328 tests, 96% line and branch coverage of `app/`**, all against real PostgreSQL
  with the real migration; nothing is mocked. They have only ever run on the
  development machine: GitHub Actions is not enabled for the repository.
- `tests/concurrency/` — one test per invariant against real PostgreSQL: hot seat (60
  contenders → one `201`, 59 × `409`, zero unhandled), per-user limit, one key fired
  20× concurrently, opposite-order multi-seat claims, reconciliation sampled
  mid-burst, lapsed-hold re-claim. Replacing the claim predicate with `true` fails
  four of the six, so they are testing the mechanism and not the happy path.
- One adversarial review round, run by a separate agent executing probes against
  PostgreSQL: it could not produce a double-sell, a deadlock, a limit breach or two
  reservations for one key across several thousand randomized attempts. It did find
  three inputs that returned a 500 where a 4xx was owed (a lock timeout on
  confirm/cancel, a NUL character, a token for a missing user); each is fixed with a
  test (LEARN-017).
- **20,000 buyers locally**, one uvicorn worker, pool of 20, 5,000 requests in flight:
  20,530 reserves in 95s — 2,975 created, 17,529 `SEAT_TAKEN`, hot seat 1 winner of
  500, 3,901 seats sold and none twice, every reconciliation sample held, and no 5xx
  and no error line from the service. One request was dropped by the client's own
  connection. Latency at that depth was poor (p50 11s): one Python process.
- Local burst, one uvicorn worker, pool of 20: 3,530 reserve requests in 9.6s with 500
  in flight — 705 created, 2,800 `SEAT_TAKEN`, 19 replays, 6 `PER_USER_LIMIT`, **zero
  5xx**, hot seat 1 winner of 500, every reconciliation check green. Latency at that
  depth: p50 0.8s, p95 3.1s. That is 500 requests sharing 20 connections and one
  Python process — requests wait rather than fail — and it was not tuned or profiled.
- **Live burst** against the Render free instance, at the script's default size with
  rate limiting on and correctly configured (`./burst.sh <URL> --concurrency 50`):
  400 buyers on 213 seats plus 150 contenders for one hot seat — 580 reserve requests,
  132 created, 423 `SEAT_TAKEN`, 19 replays, 6 `PER_USER_LIMIT`. Hot seat: 1 winner of
  150. All 24 mid-flight reconciliation samples held, no seat was sold twice,
  `/metrics` equalled the API, `unhandled_exceptions_total` did not move, zero 5xx and
  zero dropped requests. Latency at 50 in flight: p50 2.2s, p95 7.1s — a fraction of
  a shared CPU, and not tuned.
  An earlier, smaller live run had one response in 310 that was not JSON. Every
  correctness check passed on that run too and the service's fault counter did not
  move, but the cause was never established: the script at the time discarded the
  status code, and a redeploy was in progress. The script now records the status and
  whether the service or the platform's proxy answered; it has not recurred.

## Rate limiting, and the weakness it only narrows

The per-user seat limit is per *principal*, guest principals cost nothing to create,
and a reserve confirms with no payment step. On its own, then, the limit stops one
account over-buying and does nothing about one client minting accounts. The
adversarial review made that concrete: 2,500 guests from one client in seconds.

Rate limiting bounds it. Buckets are keyed by verified principal wherever a token is
present — so a crowd behind one address is never throttled as one, and a test asserts
exactly that — and by client address only for sign-in and guest creation. Guest
creation is held to one a second sustained per address.

That is a bound, not a cure. A patient client, or one with many addresses, still
accumulates seats; closing it takes something a guest cannot mint, a payment step or
a verified identity. Removing guests would not help: registration is exactly as free.

One thing only the live deployment could show. The client address is read from the
right of `X-Forwarded-For` by a configured hop count, because the left is whatever
the client sent. With the count at 1, a 429 from the deployed service named an
internal `10.x` address: the platform's own proxy, shared by every client. At 2,
700 requests in a few seconds were not limited at all: the address resolved
differently on every request. At 3 the limiter names the caller's real address, and
forging the header does not move it. Every 429 names the address it was applied to
precisely so that a wrong setting is visible from outside; that is how the first was
found, and counting requests that should have been refused is how the second was.
Neither failure would have shown in a test suite or a passing burst.

## Observability: what is built

- **`/metrics`**: reservations confirmed, declined by reason (`seat_taken`,
  `per_user_limit`, `idempotent_replay`, `lock_timeout`, …), cancelled; seats
  available per show, read from the database at scrape time with the claim's own
  predicate so it cannot disagree with the API; `unhandled_exceptions_total`, the
  direct measure of "zero 5xx"; audit written, dropped and buffered.
- **Structured logs**: single-line JSON, every line with `request_id`, declines at
  `info` so the error stream contains only faults, secrets redacted by the formatter.
  Written by a separate thread through a bounded queue, so a stalled log consumer
  cannot block a booking.
- **An audit trail**: one row per request — who, which show, which seats, status,
  duration, outcome, request id. The request path appends to an in-memory buffer and
  **never waits**: a full buffer drops the record and counts it. A writer task drains
  it in batches on its own database connection, outside the request pool. A test
  sizes the buffer to one, fires thirty bookings, and asserts all thirty succeed
  while twenty-nine records are dropped. Losing an audit row is an inconvenience;
  failing a booking is a defect.
- **An admin console** at `/admin`: the numbers above over a time window, latency per
  route from the audit trail, the trail itself with filters, and the live logs —
  click a request id in the trail to see its log lines. This is the "logs access" the
  platform does not offer publicly.
- **`/readyz`** runs a real query, fails closed, and reports whether rate limiting is
  on.

One thing the audit trail showed at once: at the innermost layer a reserve takes about
5 ms, while the same burst's clients saw hundreds. The time is spent queueing in front
of the handler, in a single Python process — not in the database and not on row locks.

## How the live service is configured

The deployed service is deliberately not configured as a production service would be,
and the differences matter to anyone testing it.

| Setting | Live | A real launch | Consequence on the live service |
|---|---|---|---|
| Rate limiting | **off** | on | A load test from one machine is never throttled. Nor is abuse: the per-user limit can be sidestepped by creating guests |
| Access token lifetime | **1 hour** | 15 minutes, with refresh | A long test does not lose its tokens mid-run. After an hour a request answers `401 UNAUTHENTICATED` and the client must sign in again — or refresh, if it registered |
| Guest token lifetime | 1 hour | 1 hour | A guest cannot refresh. The session ends |
| Admin credentials | **published in the README**, reset at every start | secret | Anyone can create or delete shows — deleting one removes every booking on it — and read the audit trail and logs. No secret is ever logged |
| Instance | free: a fraction of a CPU, sleeps when idle | sized for the on-sale | About 19 bookings a second. A 20,000-request burst will be timed out by clients and by the platform's proxy long before the service has answered it |
| Database pool | 20 | sized to the database | Requests beyond that wait for a connection rather than fail |
| Processes | 1 | several | Counters, rate-limit buckets and the log view are per process and reset on restart |

The correctness properties do not depend on any of these. What does is throughput,
and the free instance cannot be scaled from here; the same container runs anywhere
with `docker compose up`, which is how the 20,000-buyer figure below was measured.

## What is not built

The design set in [mds/](mds/00-overview.md) describes the service as it is;
[mds/17-future-scope.md](mds/17-future-scope.md) is the one place that lists what it
is not. In short:

- **A payment or identity step.** Without one the per-user limit bounds an account,
  not a person.
- **Part of the metric catalogue**: an HTTP latency histogram and pool gauges on
  `/metrics`. Latency per route is available in the admin console, from the audit
  trail, but not as a Prometheus series.
- **Taking a show off sale**, and sale windows.
- **Refresh-token revocation**; refresh is stateless.
- **A single log line for a crash**: an unhandled exception is logged twice, once
  without its request id.

And what is built but less proven than it should be: only the claim path has had an
adversarial review; the burst has run live at its default size, never near the scale
the design is sized for; the negative controls were run by hand, not as a permanent
test; the admin console has been exercised through its API and not checked in a range
of browsers; and nobody has verified the README from a clean clone on another
machine.

## What comes next

1. Turn CI on. Until then nothing has been verified off one machine.
2. Review auth, shows and the rate limiter the way the claim path was reviewed.
3. Decide how a principal earns the right to reserve — the one weakness with a
   product consequence.
4. A burst far beyond the default size against the live URL.
5. Audit, then the missing metrics.

## AI usage

Built with Claude Code. The full, contemporaneous record — including where the AI was
wrong — is [mds/16-decision-highlights.md](mds/16-decision-highlights.md).

**What the AI did.** Drafted the design documents and the correctness argument, wrote
the code and tests, and ran the failure cases. Several defects were found only by
executing rather than inspecting: secrets printed on a boot failure, a harness that
deadlocked on a barrier wider than its pool, and — in the final build — a migration
that ran locally and failed inside the container image.

**What was directed, and where direction changed the design.** The decisions that
shaped the service were made by pushing back on the AI's recommendations, not by
accepting them:

- *"Reason with me and then perform"* stopped a delegation mid-flight and produced a
  better design than the one already recommended: lazy expiry with **no sweeper**.
- *"Should we keep holds as well?"* turned a binary into the shipped model: confirm by
  default, holds opt-in.
- *"Never over-engineer"* cut an `events` table and seat-coordinate columns no
  requirement drove; *"we should not under-deliver"* stopped an external review's
  cuts from going too far.

**Where it cost time.** A multi-agent workflow produced a rigorous design slowly, and
the implementation was compressed into a final direct pass as a result. The design
work is why that pass was possible; the lesson is that the proof of this service is
one SQL statement and its tests, and those should have existed on day one.

**How it was verified rather than trusted.** The correctness argument was written down
so it could be checked against the SQL it claimed to prove — which is how four design
defects were found. The mechanism was probed against real PostgreSQL before any
application code, and the race tests are run against a deliberately broken variant to
confirm they fail.
