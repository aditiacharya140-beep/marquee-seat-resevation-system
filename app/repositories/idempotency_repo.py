"""The key row is the ownership token; `uq_idem_user_key` decides who owns it.

`try_claim`, `get` and `reclaim_if_stale` must be called on a connection that is NOT in
a transaction, so each commits at once: ownership has to be visible to a concurrent
duplicate immediately. `complete` is the opposite — it must run inside the claim's
transaction, so the reservation and the record of it commit together or not at all.
"""

import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from app.core.constants import IdempotencyState
from app.domain.models import IdempotencyRecord
from app.repositories.base import current_request_id


async def try_claim(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    key: str,
    scope: str,
    fingerprint: str,
    retention_hours: int,
) -> UUID | None:
    """The key's id if this request now owns it, `None` if someone else already does."""
    key_id: UUID | None = await conn.fetchval(
        """
        INSERT INTO idempotency_keys (id, user_id, key, scope, request_fingerprint, state,
                                      expires_at, request_id)
        VALUES ($1, $2, $3, $4, $5, $6, now() + ($7::int * INTERVAL '1 hour'), $8)
        ON CONFLICT (user_id, key) DO NOTHING
        RETURNING id
        """,
        uuid4(),
        user_id,
        key,
        scope,
        fingerprint,
        IdempotencyState.IN_PROGRESS.value,
        retention_hours,
        current_request_id(),
    )
    return key_id


async def get(
    conn: asyncpg.Connection, user_id: UUID, key: str, stale_seconds: int
) -> IdempotencyRecord | None:
    row = await conn.fetchrow(
        """
        SELECT id, request_fingerprint, state, response_body,
               (state = 'in_progress'
                AND created_at < now() - ($3::int * INTERVAL '1 second')) AS stale
          FROM idempotency_keys
         WHERE user_id = $1 AND key = $2
        """,
        user_id,
        key,
        stale_seconds,
    )
    if row is None:
        return None
    return IdempotencyRecord(
        id=row["id"],
        fingerprint=row["request_fingerprint"],
        state=IdempotencyState(row["state"]),
        response_body=json.loads(row["response_body"]) if row["response_body"] else None,
        stale=row["stale"],
    )


async def reclaim_if_stale(
    conn: asyncpg.Connection, key_id: UUID, stale_seconds: int
) -> UUID | None:
    """Take over a key whose owner is presumed dead, returning the key's NEW id.

    The id is the ownership token, so taking over rotates it: if the old owner is in
    fact alive, its `lock_owned`, `complete` and `release` all address an id that no
    longer exists and it can neither commit a reservation nor delete the new owner's
    key. One guarded UPDATE, so of several reclaimers exactly one wins. An owner that
    has reached T2 holds the row lock, so this waits for it; if that wait times out the
    owner is plainly alive and nothing is reclaimed.
    """
    try:
        reclaimed: UUID | None = await conn.fetchval(
            """
            UPDATE idempotency_keys
               SET id = $4, created_at = now(), request_id = $3
             WHERE id = $1 AND state = 'in_progress'
               AND created_at < now() - ($2::int * INTERVAL '1 second')
            RETURNING id
            """,
            key_id,
            stale_seconds,
            current_request_id(),
            uuid4(),
        )
    except asyncpg.LockNotAvailableError:
        return None
    return reclaimed


async def lock_owned(conn: asyncpg.Connection, key_id: UUID) -> bool:
    """First statement of T2: prove this request still owns the key, and hold the row
    so nobody can take it over until T2 ends. `False` means it was reclaimed."""
    try:
        row = await conn.fetchrow(
            "SELECT 1 FROM idempotency_keys WHERE id = $1 AND state = $2 FOR UPDATE",
            key_id,
            IdempotencyState.IN_PROGRESS.value,
        )
    except asyncpg.LockNotAvailableError:
        return False
    return row is not None


async def complete(
    conn: asyncpg.Connection,
    *,
    key_id: UUID,
    status_code: int,
    response_body: dict[str, Any],
    reservation_id: UUID,
) -> None:
    await conn.execute(
        """
        UPDATE idempotency_keys
           SET state = $2, status_code = $3, response_body = $4::jsonb,
               reservation_id = $5, completed_at = now()
         WHERE id = $1
        """,
        key_id,
        IdempotencyState.COMPLETED.value,
        status_code,
        json.dumps(response_body),
        reservation_id,
    )


async def release(conn: asyncpg.Connection, key_id: UUID) -> None:
    """Only successes are stored (ADR-020): a decline or a fault gives the key back, so
    a retry is a genuine new attempt. Guarded on state so it can never delete the
    record of a reservation that did commit."""
    await conn.execute(
        "DELETE FROM idempotency_keys WHERE id = $1 AND state = $2",
        key_id,
        IdempotencyState.IN_PROGRESS.value,
    )
