from datetime import datetime
from uuid import UUID

import asyncpg

from app.core.constants import ReservationStatus
from app.db.sql import RESERVATION_EFFECTIVE_STATUS
from app.domain.models import ClaimedSeat, Reservation
from app.repositories.base import contention_is_a_decline, current_request_id
from app.repositories.seat_repo import backstop_violation

_COLUMNS = """id, show_id, user_id, amount_paise, currency, hold_expires_at,
              confirmed_at, cancelled_at, created_at"""


def _reservation(row: asyncpg.Record, status: str, labels: list[str]) -> Reservation:
    return Reservation(
        id=row["id"],
        show_id=row["show_id"],
        user_id=row["user_id"],
        status=ReservationStatus(status),
        labels=labels,
        amount_paise=row["amount_paise"],
        currency=row["currency"],
        expires_at=row["hold_expires_at"],
        confirmed_at=row["confirmed_at"],
        cancelled_at=row["cancelled_at"],
        created_at=row["created_at"],
    )


async def close_superseded_claims(conn: asyncpg.Connection, seat_ids: list[UUID]) -> int:
    """Close claim rows left active by holds that lapsed (ADR-019, Mechanism 3).

    Only sound AFTER the claim, when this transaction holds the lock on every one of
    these seats and the claim predicate has proved none had a live claim. And it must
    stay its own statement: folded into the insert as a CTE the two would share one
    snapshot with no defined order, and the insert could hit the index first (LEARN-009).
    """
    with contention_is_a_decline():
        result: str = await conn.execute(
            """
            UPDATE reservation_seats SET released_at = now()
             WHERE seat_id = ANY($1::uuid[]) AND released_at IS NULL
            """,
            seat_ids,
        )
    return int(result.split()[-1])


async def create(
    conn: asyncpg.Connection,
    *,
    reservation_id: UUID,
    show_id: UUID,
    user_id: UUID,
    seats: list[ClaimedSeat],
    currency: str,
    ttl_or_none: int | None,
    idempotency_key_id: UUID,
) -> Reservation:
    """`now()` is transaction-stable, so the expiry written here is the same instant
    the claim wrote on the seats (LEARN-007)."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO reservations (id, show_id, user_id, status, seat_count, amount_paise,
                                  currency, hold_expires_at, confirmed_at,
                                  idempotency_key_id, request_id)
        VALUES ($1, $2, $3,
                CASE WHEN $7::int IS NULL THEN 'confirmed' ELSE 'held' END,
                $4, $5, $6,
                CASE WHEN $7::int IS NULL THEN NULL
                     ELSE now() + ($7::int * INTERVAL '1 second') END,
                CASE WHEN $7::int IS NULL THEN now() END,
                $8, $9)
        RETURNING {_COLUMNS}, status
        """,
        reservation_id,
        show_id,
        user_id,
        len(seats),
        sum(seat.price_paise for seat in seats),
        currency,
        ttl_or_none,
        idempotency_key_id,
        current_request_id(),
    )
    try:
        await conn.execute(
            """
            INSERT INTO reservation_seats (reservation_id, seat_id, label, price_paise)
            SELECT $1, seat.id, seat.label, seat.price_paise
              FROM unnest($2::uuid[], $3::text[], $4::bigint[]) AS seat (id, label, price_paise)
            """,
            reservation_id,
            [seat.id for seat in seats],
            [seat.label for seat in seats],
            [seat.price_paise for seat in seats],
        )
    except asyncpg.UniqueViolationError:
        raise backstop_violation() from None
    return _reservation(row, row["status"], [seat.label for seat in seats])


async def get_owned(
    conn: asyncpg.Connection, reservation_id: UUID, user_id: UUID
) -> Reservation | None:
    """Ownership is in the WHERE clause, so a non-owner and a missing row are the same
    answer. There is deliberately no lookup by id alone."""
    row = await conn.fetchrow(
        f"""
        SELECT {_COLUMNS}, {RESERVATION_EFFECTIVE_STATUS} AS effective_status,
               ARRAY(SELECT label FROM reservation_seats
                      WHERE reservation_id = reservations.id ORDER BY label) AS labels
          FROM reservations
         WHERE id = $1 AND user_id = $2
        """,
        reservation_id,
        user_id,
    )
    return _reservation(row, row["effective_status"], list(row["labels"])) if row else None


async def list_for_user(
    conn: asyncpg.Connection,
    user_id: UUID,
    *,
    show_id: UUID | None,
    status: str | None,
    after: tuple[datetime, UUID] | None,
    limit: int,
) -> list[Reservation]:
    """Owner-scoped by construction. `status` filters on the effective status, so a
    lapsed hold is found under `expired` and never under `held`."""
    rows = await conn.fetch(
        f"""
        SELECT {_COLUMNS}, {RESERVATION_EFFECTIVE_STATUS} AS effective_status,
               ARRAY(SELECT label FROM reservation_seats
                      WHERE reservation_id = reservations.id ORDER BY label) AS labels
          FROM reservations
         WHERE user_id = $1
           AND ($2::uuid IS NULL OR show_id = $2)
           AND ($3::text IS NULL OR {RESERVATION_EFFECTIVE_STATUS} = $3)
           AND ($4::timestamptz IS NULL OR (created_at, id) < ($4::timestamptz, $5::uuid))
         ORDER BY created_at DESC, id DESC
         LIMIT $6
        """,
        user_id,
        show_id,
        status,
        after[0] if after else None,
        after[1] if after else None,
        limit,
    )
    return [_reservation(row, row["effective_status"], list(row["labels"])) for row in rows]


async def cancel_owned(conn: asyncpg.Connection, reservation_id: UUID, user_id: UUID) -> bool:
    """The decision for a cancel: one guarded UPDATE whose row count is the answer.
    A confirmed booking is cancellable by its owner, as is a live hold; a lapsed hold
    is not — its seats are already effectively available (ADR-040)."""
    return await _decide(
        conn,
        """
        UPDATE reservations
           SET status = 'cancelled', cancelled_at = now(), hold_expires_at = NULL,
               updated_at = now(), request_id = $3
         WHERE id = $1 AND user_id = $2
           AND (status = 'confirmed' OR (status = 'held' AND hold_expires_at > now()))
        RETURNING id
        """,
        reservation_id,
        user_id,
    )


async def confirm_owned(conn: asyncpg.Connection, reservation_id: UUID, user_id: UUID) -> bool:
    return await _decide(
        conn,
        """
        UPDATE reservations
           SET status = 'confirmed', confirmed_at = now(), hold_expires_at = NULL,
               updated_at = now(), request_id = $3
         WHERE id = $1 AND user_id = $2
           AND status = 'held' AND hold_expires_at > now()
        RETURNING id
        """,
        reservation_id,
        user_id,
    )


async def _decide(
    conn: asyncpg.Connection, statement: str, reservation_id: UUID, user_id: UUID
) -> bool:
    # Waits on the reservation row if the owner double-submitted, so it can time out.
    with contention_is_a_decline():
        row = await conn.fetchrow(statement, reservation_id, user_id, current_request_id())
    return row is not None


async def close_claims_for_reservation(conn: asyncpg.Connection, reservation_id: UUID) -> None:
    with contention_is_a_decline():
        await conn.execute(
            """
            UPDATE reservation_seats SET released_at = now()
             WHERE reservation_id = $1 AND released_at IS NULL
            """,
            reservation_id,
        )
