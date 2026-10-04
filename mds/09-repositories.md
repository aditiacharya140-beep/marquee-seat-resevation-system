# Repositories

Every SQL statement the service issues lives in `app/repositories/`, one module per aggregate. Tests are the one exception: a few assert what the database *refuses*, and say so.

## Contract

A repository function:

- **Receives a connection** from the caller. It never opens, commits or rolls back a transaction. Transaction boundaries belong to services, because only a service knows which statements must succeed or fail together.
- Takes and returns plain values and the dataclasses in `domain/models.py`, never a request model and never a `Request`.
- **Translates driver exceptions** into `AppError` subclasses before returning ([08-error-logging.md](08-error-logging.md)).
- **Stamps `request_id`** from the context var on the rows it writes, without receiving it as a parameter (`base.current_request_id`).
- Binds every value as a parameter. Statements are composed only from the module constants in `db/sql.py`, at import time; a value is never interpolated.

## Why services own transactions

The reserve path is correct only because one specific set of statements is atomic: the key lock, the quota lock, the limit check, the seat claim, the superseded-row closure, the reservation insert and the key completion. If `claim_many` opened its own transaction that set would fragment, and a failure between two of them would leave seats claimed with no reservation.

## Modules

### `base.py`

`current_request_id()`, and `contention_is_a_decline()` — a context manager that turns `lock_not_available` and `deadlock_detected` into 409 `SEAT_TAKEN`. It wraps **every** statement on the reserve, confirm and cancel paths that can wait on a row lock, not only the claim: an unwrapped one turns a slow neighbour into a 500 (LEARN-017).

Deliberately not a generic `Repository[T]`. The interesting operations here are not CRUD, and forcing them through a generic interface would hide exactly the detail the design depends on.

### `user_repo.py`

```
create_user(conn, email, password_hash, role)        -> User
create_guest(conn)                                    -> User
upgrade_guest(conn, user_id, email, password_hash)    -> User | None
upsert_admin(conn, email, password_hash)              -> None
get_by_email(conn, email)                             -> User | None
get_by_id(conn, user_id)                              -> User | None
admin_exists(conn)                                    -> bool
```

Email uniqueness is the index's decision, never a prior existence check: a duplicate surfaces as 409 `EMAIL_TAKEN` from `uq_users_email`. `upgrade_guest` is one guarded `UPDATE ... WHERE id = $1 AND is_guest = true`, so two concurrent upgrades have one winner and `None` means the row was not a guest.

### `show_repo.py`

```
create_show(conn, *, name, …, labels, seat_prices, seat_sections)  -> Show
get_show(conn, show_id)                                            -> Show | None
list_shows(conn, *, status, event_kind, after, limit)              -> list[Show]
list_seats(conn, show)                                             -> list[SeatView]
available_by_show(conn, max_shows)                                 -> dict[UUID, int]
delete_show(conn, show_id)                                         -> dict[str, int] | None
```

`create_show` inserts every seat in one statement from `unnest` arrays. `list_seats` returns every seat with its **effective** status from one statement; the counts in `GET /shows/{id}` are tallied from those same rows, which is why they cannot disagree with the seat list or fail to sum to the total. `list_shows` is keyset-paginated on `(created_at, id)` and never touches `seats`. `available_by_show` feeds the gauge at scrape time, using the claim's own predicate. `delete_show` locks the show row first, then removes its reservations, their claim rows and idempotency keys, and the show; seats and quota rows cascade. Every claim holds a key-share lock on the show row from its quota insert, so a delete waits for claims in flight and later claims find no show (ADR-043).

### `seat_repo.py`

The most important module in the service.

```
lock_quota(conn, user_id, show_id)                                      -> None
count_active_for_user(conn, show_id, user_id)                           -> int
claim_many(conn, *, show_id, labels, user_id, reservation_id,
           ttl_or_none, show_price_paise)                               -> list[ClaimedSeat]
labels_not_in_show(conn, show_id, labels)                               -> list[str]
release_for_reservation(conn, reservation_id)                           -> list[str]
confirm_for_reservation(conn, reservation_id)                           -> list[str]
```

`ttl_or_none` is the **one** parameter that selects the target state: `None` claims straight to `confirmed` with no expiry, an integer holds for that many seconds (ADR-017). The statement derives both `status` and `hold_expires_at` from it, so the two cannot be passed inconsistently.

`claim_many` returns what it claimed; the **service** compares the count to the request and rolls back. A single-seat request goes through the same statement — there is no separate `claim_one`. `labels_not_in_show` is diagnosis after a shortfall, to tell 404 from 409; it is never part of the decision.

`release_for_reservation` and `confirm_for_reservation` are the ordered `FOR UPDATE` CTEs of ADR-022, guarded on `reservation_id`, with `ORDER BY label`. Confirm requires a live hold; release requires the seat to be active — confirmed, or a live hold.

There is no `sweep_expired`. There is no sweeper (ADR-017).

### `reservation_repo.py`

```
close_superseded_claims(conn, seat_ids)                       -> int
create(conn, *, reservation_id, show_id, user_id, seats, …)   -> Reservation
get_owned(conn, reservation_id, user_id)                      -> Reservation | None
list_for_user(conn, user_id, *, show_id, status, after, limit)-> list[Reservation]
cancel_owned(conn, reservation_id, user_id)                   -> bool
confirm_owned(conn, reservation_id, user_id)                  -> bool
close_claims_for_reservation(conn, reservation_id)            -> None
```

`get_owned` and `list_for_user` take the owner as part of the `WHERE` clause. There is no `get(reservation_id)` to accidentally use without an ownership filter — the unsafe method does not exist, which is stronger than remembering to filter. Both apply the reservation effective-status expression, so a lapsed hold reads `expired`, never `held`.

`cancel_owned` and `confirm_owned` are the guarded `UPDATE`s on the `reservations` row, with the owner in the `WHERE` clause. Confirm matches a live hold; cancel matches a live hold **or a confirmed reservation** (ADR-040). `False` is the decision; the service then calls `get_owned` to choose the decline code, which is diagnosis and not control.

`close_superseded_claims` is Mechanism 3 (ADR-019): called **after** the claim and **before** the insert, in T2. It must never be folded into the insert's statement (LEARN-009).

### `idempotency_repo.py`

```
try_claim(conn, *, user_id, key, scope, fingerprint, retention_hours)  -> UUID | None
get(conn, user_id, key, stale_seconds)                                 -> IdempotencyRecord | None
reclaim_if_stale(conn, key_id, stale_seconds)                          -> UUID | None
lock_owned(conn, key_id)                                               -> bool
complete(conn, *, key_id, status_code, response_body, reservation_id)  -> None
release(conn, key_id)                                                  -> None
```

Which transaction each runs in is the whole point of the module:

- `try_claim`, `get`, `reclaim_if_stale` and `release` are called on a connection that is **not** in a transaction, so each commits at once. Ownership has to be visible to a concurrent duplicate immediately.
- `lock_owned` and `complete` run **inside T2**. `lock_owned` is its first statement; `complete` makes the reservation and the record of it commit together.

The key row's `id` is the ownership token. `reclaim_if_stale` **rotates** it, so an owner presumed dead but merely slow finds its id gone at `lock_owned` and stops before touching a seat; its `release` then matches nothing (ADR-033).

`release` deletes the row, guarded on `state = 'in_progress'`, and is called on every rolled-back T2 — a decline as well as a fault (ADR-020). `try_claim` translates a foreign-key violation to 401: that is where a validly signed token for a user with no row first touches the database.

A retention purge is not written; see [17-future-scope.md](17-future-scope.md).

### `audit_repo.py`

```
insert_batch(conn, records)                                      -> None
list_recent(conn, *, since, before_id, status_code, …, limit)   -> list[dict]
status_classes(conn, since)                                      -> dict[str, int]
outcomes(conn, since, limit)                                     -> list[dict]
routes(conn, since, limit)                                       -> list[dict]
per_minute(conn, since)                                          -> list[dict]
```

`insert_batch` is called only by the audit writer, on its dedicated connection, never inside a request's transaction. Every read takes a `since` and is served by `ix_audit_occurred`, so no console query can scan the whole table.

### `health_repo.py`

`ping(conn)` — the `SELECT 1` behind `/readyz`.

---

## Connection pooling

One `asyncpg` pool (`db/engine.py`), created in the lifespan and closed on shutdown. `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout` and `TimeZone=UTC` are passed as **startup parameters**, not issued as `SET`: the pool runs `RESET ALL` when a connection is released, which reverts a `SET` and returns to these (LEARN-014).

`db/session.py` gives services two scopes, `acquire()` and `transaction()`, and translates what "the database is not there" looks like — a pool-acquire timeout, a connection failure, a statement cancelled by `statement_timeout` — into 503 `DATABASE_UNAVAILABLE`. A value the database cannot represent (`DataError`) becomes a 422 there as a backstop behind the schemas.

Pool size is bounded by the **database's** connection ceiling, not by expected request concurrency, and validated against it at startup with one connection kept back. Numbers are in [11-scalability.md](11-scalability.md).

---

## Prohibited patterns

| Pattern | Why |
|---|---|
| Read-then-write on seat state | Double-sells under load. The guarded update exists for this. |
| `SELECT` existence before `INSERT` for a unique column | Races. Let the constraint decide and translate the violation. |
| Transaction control inside a repository | Fragments atomicity the service requires. |
| A value interpolated into SQL | Injection, and it defeats statement caching. |
| A loop of single-row inserts | N round trips where one statement suffices. |
| `SELECT *` | A new column silently changes every mapping. |
| An unfiltered get-by-id on an owned resource | One forgotten check from an authorization bypass. |
| Re-typing an effective-status expression | Divergent copies break reconciliation. |
| `SKIP LOCKED` anywhere | Declines seats that are still available. |
| An unordered `UPDATE` over a reservation's seats | Locks in scan order and leaves the path outside the deadlock proof (ADR-022). |
| A release or confirm with no `hold_expires_at > now()` guard | Promotes or releases a lapsed hold. |
| A statement that can wait on a row lock, outside `contention_is_a_decline()` | A lock timeout becomes a 500 (LEARN-017). |
| Holding a connection across an idempotency poll | Exhausts the pool under burst (ADR-026). |
| Folding the superseded-row closure into the insert | Data-modifying CTEs share a snapshot with no defined order (LEARN-009). |
| Raising a driver exception past this layer | The route cannot know what the constraint means. |

One known imperfection against the fourth-from-top rule's spirit: the fragment `status = 'held' AND hold_expires_at > now()` is written out in the release and confirm CTEs and in `list_seats`, rather than imported. The copies agree, and are quoted verbatim from [04-concurrency-and-atomicity.md](04-concurrency-and-atomicity.md).
