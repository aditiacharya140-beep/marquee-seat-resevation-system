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
list_shows(cursor, limit, filters)  -> Page[ShowSummary]
```

`create_show` inserts seats with one multi-row statement — a loop of inserts for a 2,000-seat hall is 2,000 round trips. `get_counts` is the single-snapshot query from [03-data-model.md](03-data-model.md), which is what makes the reconciliation invariant hold by construction. `list_shows` never scans seats; it reads precomputed counts per show.

### `seat_repo.py`

The most important module in the service.

```
claim_one(conn, show_id, label, user_id, reservation_id, ttl, show_price)   -> ClaimedSeat | None
claim_many(conn, show_id, labels, user_id, reservation_id, ttl, show_price) -> list[ClaimedSeat]
count_active_for_user(conn, show_id, user_id)                              -> int
release_for_reservation(conn, reservation_id)                              -> list[str]
confirm_for_reservation(conn, reservation_id)                              -> list[str]
sweep_expired(conn, batch_size)                                            -> list[SweptSeat]
labels_not_in_show(conn, show_id, labels)                                  -> list[str]
```

`claim_one` returns `None` on zero rows affected — the decline. `claim_many` returns whatever it claimed and the **service** compares the count to the request and rolls back; the repository does not decide policy, it reports what the statement did.

Every statement here is quoted verbatim in [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md). They are the same text. A change to one is a change to the other, in the same commit.

The effective-status expression is imported from `db/sql.py`. It is not retyped in this module, because a second copy that drifts breaks reconciliation.

### `reservation_repo.py`

```
create(conn, reservation, seats)                       -> Reservation
get_owned(conn, reservation_id, user_id)               -> Reservation | None
mark_confirmed(conn, reservation_id)                   -> bool
mark_cancelled(conn, reservation_id)                   -> bool
mark_expired(conn, reservation_ids)                    -> int
list_for_user(conn, user_id, filters, cursor, limit)   -> Page[Reservation]
```

`get_owned` takes the owner as part of the `WHERE` clause. There is no `get(reservation_id)` to accidentally use without an ownership filter — the unsafe method does not exist, which is stronger than remembering to filter. The `mark_*` methods are guarded updates returning whether the transition applied, so a service can distinguish "already in that state" from "no longer yours".

### `idempotency_repo.py`

```
try_claim(key, user_id, scope, fingerprint)       -> ClaimOutcome   # own | existing row
get(user_id, key)                                 -> IdemKey | None
complete(conn, key_id, status_code, response_body, reservation_id) -> None
release(key_id)                                   -> None
reclaim_if_stale(user_id, key, fingerprint, max_age) -> ClaimOutcome
purge_expired(batch_size)                         -> int
```

`try_claim` runs in its own transaction, because ownership must be visible to concurrent duplicates immediately. `complete` takes a connection, because it must commit inside the reservation's transaction. The split signature enforces that distinction at the type level rather than by convention — the argument for it is in [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md).

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
| Re-typing the effective-status expression | Divergent copies break reconciliation. |
| `SKIP LOCKED` in the claim path | Declines seats that are still available. Sweeper only. |
| Raising a driver exception past this layer | The route cannot know what the constraint means. |
| Returning an ORM-ish lazy object | Hides IO behind attribute access, outside any transaction. |
