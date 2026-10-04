# Repositories

Every SQL statement in the service lives in `app/repositories/`. One module per aggregate. No SQL anywhere else — not in a service, not in a route, not in a helper, not in a test fixture that bypasses the layer.

## Contract

A repository method:

- Receives a connection or transaction handed to it by the caller. It **never** opens, commits, or rolls back one. Transaction boundaries belong to services, because only a service knows which statements must succeed or fail together.
- Takes and returns domain objects or plain values, never Pydantic request models and never `Request`.
- Translates driver exceptions into `AppError` subclasses before returning, per the table in [08-error-logging.md](08-error-logging.md).
- Stamps `request_id` from the context var on every insert and every update, without receiving it as a parameter.
- Uses bound parameters exclusively. No f-string interpolation into SQL, ever, including for identifiers and `LIMIT` values.

## Why services own transactions

The reserve path is correct only because a specific set of statements is atomic: the quota lock, the limit check, the seat claim, the reservation insert, and the idempotency completion. If `seat_repo.claim` opened its own transaction, that set would fragment and a failure between two of them would leave seats held with no reservation. The repository exposes the statement; the service decides what it is atomic with.

## Modules

### `base.py`

Shared mechanics, not a generic CRUD abstraction: `request_id` stamping, driver-error translation, row-to-model mapping, a cursor codec for keyset pagination, and a helper for `RETURNING`-based row-count decisions.

Deliberately not a generic `Repository[T]` with `get/list/create/update/delete`. The interesting operations here — a guarded claim, an ordered multi-row CTE, a derived limit count — are not CRUD, and forcing them through a generic interface obscures exactly the detail the design depends on.

### `user_repo.py`

```
create_user(email, password_hash, role)       -> User
create_guest()                                -> User
get_by_email(email)                           -> User | None
get_by_id(user_id)                            -> User | None
upgrade_guest(user_id, email, password_hash)  -> User | None
admin_exists()                                -> bool
```

`get_by_email` lowercases to match `uq_users_email`. `upgrade_guest` is the guarded update from [05-auth-and-rbac.md](05-auth-and-rbac.md) and returns `None` when the row was not a guest, letting the service distinguish `ALREADY_REGISTERED` from a missing user. Email collisions surface as `EMAIL_TAKEN` from the unique constraint, never from a prior existence check.

### `show_repo.py`

```
create_show(show, seats)            -> Show      # show + all seat rows, one transaction
get_show(show_id)                   -> Show | None
get_show_for_sale(show_id)          -> Show | None
get_counts(show_id)                 -> SeatCounts
list_seats(show_id)                 -> list[Seat]
```

`create_show` inserts seats with one multi-row statement — a loop of inserts for a 2,000-seat hall is 2,000 round trips. `get_counts` is the single-snapshot query from [03-data-model.md](03-data-model.md), which is what makes the reconciliation invariant hold by construction. `currency` and `event_kind` are always supplied by the caller, never defaulted by the column, so there is one source for each (ADR-025). The paginated `list_shows` is deferred (ADR-015); nothing depends on it, so the cursor codec is not built yet.

### `seat_repo.py`

The most important module in the service.

```
claim_one(conn, show_id, label, user_id, reservation_id, ttl_or_none, show_price)   -> ClaimedSeat | None
claim_many(conn, show_id, labels, user_id, reservation_id, ttl_or_none, show_price) -> list[ClaimedSeat]
count_active_for_user(conn, show_id, user_id)                                      -> int
release_for_reservation(conn, reservation_id)                                      -> list[str]
confirm_for_reservation(conn, reservation_id)                                      -> list[str]
labels_not_in_show(conn, show_id, labels)                                          -> list[str]
```

`ttl_or_none` is the **one** parameter that selects the target state: `None` means claim straight to `confirmed` with no expiry, an integer means hold for that many seconds (ADR-017). The statement derives both `status` and `hold_expires_at` from it, so the two cannot be passed inconsistently. The parameter name says so, because `ttl=None` reading as "no TTL, so use a default" is exactly the misreading that would reintroduce holds everywhere.

`claim_one` returns `None` on zero rows affected — the decline. `claim_many` returns whatever it claimed and the **service** compares the count to the request and rolls back; the repository does not decide policy, it reports what the statement did.

`release_for_reservation` and `confirm_for_reservation` are the ordered `FOR UPDATE` CTEs from ADR-022, guarded on `reservation_id` **and** `hold_expires_at > now()`, with `ORDER BY label`. They are not plain `UPDATE ... WHERE reservation_id = $1` statements: that form locks in scan order and put cancel outside the deadlock proof, and it omitted the expiry guard, which with lazy-only expiry meant a lapsed hold stayed promotable.

`sweep_expired` does not exist. There is no sweeper (ADR-017).

Every statement here is quoted verbatim in [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md). They are the same text. A change to one is a change to the other, in the same commit.

Both effective-status expressions are imported from `db/sql.py`. Neither is retyped in this module, because a second copy that drifts breaks reconciliation.

### `reservation_repo.py`

```
create(conn, reservation, seats)                            -> Reservation
close_superseded_claims(conn, seat_ids)                     -> int
get_owned(conn, reservation_id, user_id)                    -> Reservation | None
confirm_owned(conn, reservation_id, user_id)                -> Reservation | None
cancel_owned(conn, reservation_id, user_id)                 -> Reservation | None
close_claims_for_reservation(conn, reservation_id)          -> int
list_for_user(conn, user_id, filters, cursor, limit)        -> Page[Reservation]
```

`get_owned` takes the owner as part of the `WHERE` clause. There is no `get(reservation_id)` to accidentally use without an ownership filter — the unsafe method does not exist, which is stronger than remembering to filter. It applies the reservation effective-status expression, so a lapsed hold is returned as `expired`, never as `held`.

`confirm_owned` and `cancel_owned` are the guarded `UPDATE`s on the `reservations` row from ADR-022: owner and `status='held' AND hold_expires_at > now()` in the `WHERE` clause, returning the row or `None`. They replace `mark_confirmed(conn, reservation_id)` and `mark_cancelled(conn, reservation_id)`, which took no owner — a signature that made the ownership check something a service had to remember rather than something the statement enforced. `None` is the decision; the service then calls `get_owned` to choose the decline code, which is diagnosis and not control (ADR-022).

`close_superseded_claims` is Mechanism 3 (ADR-019): it closes any active `reservation_seats` row for the given seat ids, called **after** the claim and **before** the insert, in T2. It returns the number of rows closed, which the service turns into `superseded_claims_closed_total`. It must never be folded into the insert's statement — LEARN-009 explains why.

`mark_expired` does not exist. Nothing rewrites a lapsed hold's stored status; readers derive it.

### `idempotency_repo.py`

```
try_claim(key, user_id, scope, fingerprint)       -> ClaimOutcome   # own | existing row
get(user_id, key)                                 -> IdemKey | None
complete(conn, key_id, status_code, response_body, reservation_id) -> None
release(key_id)                                   -> None
reclaim_if_stale(user_id, key, fingerprint, max_age) -> ClaimOutcome
purge_expired(batch_size)                         -> int
```

Three signatures carry an argument, and the argument is the reason for the signature:

- `try_claim` takes **no connection**: it runs in its own transaction, because ownership must be visible to concurrent duplicates immediately.
- `complete` **takes a connection**: it must commit inside the reservation's transaction, or a crash could leave a committed reservation whose key says `in_progress`.
- `get` takes **no connection**, which is what enforces ADR-026: the in-progress waiter acquires a connection, performs one point read on `uq_idem_user_key`, releases it, and only then sleeps. A waiter that held its connection across the wait budget would exhaust the pool under burst and produce exactly the 503s ADR-016 forbids, so this is a correctness property of the signature and not a style choice.

`release` deletes the row and is called on **every** rolled-back T2 — a domain decline as well as a fault (ADR-020). Only successes are ever stored, so `complete` is only ever called with a 2xx `status_code`.

`purge_expired` exists for scheduled retention maintenance, not for a background loop; there is no worker that calls it (RISK-007).

### `audit_repo.py`

```
insert_batch(conn, records) -> int
```

One method. Called only by `workers/audit_writer.py`, on a dedicated connection, never inside a request transaction. Uses `executemany` over a multi-row `INSERT`, with no foreign keys to check and no `RETURNING`.

---

## Connection pooling

`asyncpg` pool, created in the application lifespan and closed on shutdown. Every connection, on acquisition, has `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, and `TimeZone=UTC` applied from config.

Sizing: pool size is bounded by the **database's** connection ceiling divided by the instance count, not by expected request concurrency. Postgres connections are expensive; a pool larger than the database can serve converts a queue into refusals. Requests queue at the pool with a bounded acquire timeout; exceeding it is 503 `DATABASE_UNAVAILABLE`, which is why pool sizing is a correctness concern for the "zero 5xx" requirement and not merely a performance tuning knob. Numbers and the arithmetic are in [11-scalability.md](11-scalability.md).

The audit writer holds its own connection outside the request pool, so a saturated request pool cannot stall audit flushing and a backed-up audit flush cannot starve requests.

---

## Prohibited patterns

| Pattern | Why |
|---|---|
| Read-then-write on seat state | Double-sells under load. The guarded update exists for this. |
| `SELECT` existence before `INSERT` for a unique column | Races. Let the constraint decide and translate the violation. |
| Transaction control inside a repository | Fragments atomicity the service requires. |
| String-interpolated SQL | Injection, and it defeats statement caching. |
| A loop of single-row inserts | N round trips where one statement suffices. |
| `SELECT *` | A new column silently changes every mapping. |
| Unfiltered `get_by_id` on an owned resource | One forgotten check from an authorization bypass. |
| Re-typing either effective-status expression | Divergent copies break reconciliation. |
| `SKIP LOCKED` anywhere | Declines seats that are still available. With no sweeper (ADR-017) it has no remaining legitimate use. |
| An unordered `UPDATE` over a reservation's seats | Locks in scan order and leaves cancel and confirm outside the deadlock proof (ADR-022). |
| A release or confirm with no `hold_expires_at > now()` guard | Promotes or releases a lapsed hold. With lazy-only expiry nothing else would catch it. |
| Holding a connection across an idempotency poll | Exhausts the pool under burst (ADR-026). |
| Folding the superseded-row closure into the insert statement | Data-modifying CTEs share a snapshot with no defined order (LEARN-009). |
| Raising a driver exception past this layer | The route cannot know what the constraint means. |
| Returning an ORM-ish lazy object | Hides IO behind attribute access, outside any transaction. |
