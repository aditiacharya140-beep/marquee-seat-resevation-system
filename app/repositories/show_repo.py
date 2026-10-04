from uuid import UUID, uuid4

import asyncpg

from app.core.constants import SeatStatus, ShowStatus
from app.db.sql import SEAT_CLAIMABLE, SEAT_EFFECTIVE_STATUS
from app.domain.models import SeatView, Show
from app.repositories.base import current_request_id

_SHOW_COLUMNS = """id, name, event_kind, price_paise, currency, per_user_limit,
                   hold_ttl_seconds, total_seats, status, created_at"""


def _show(row: asyncpg.Record) -> Show:
    return Show(
        id=row["id"],
        name=row["name"],
        event_kind=row["event_kind"],
        price_paise=row["price_paise"],
        currency=row["currency"],
        per_user_limit=row["per_user_limit"],
        hold_ttl_seconds=row["hold_ttl_seconds"],
        total_seats=row["total_seats"],
        status=ShowStatus(row["status"]),
        created_at=row["created_at"],
    )


async def create_show(
    conn: asyncpg.Connection,
    *,
    name: str,
    event_kind: str,
    price_paise: int,
    currency: str,
    per_user_limit: int,
    hold_ttl_seconds: int,
    labels: list[str],
) -> Show:
    """The show and every seat row. Seats are never inserted or deleted again, which is
    half of why `available + held + confirmed == total_seats` holds structurally."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO shows (id, name, event_kind, price_paise, currency, per_user_limit,
                           hold_ttl_seconds, total_seats, status, request_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING {_SHOW_COLUMNS}
        """,
        uuid4(),
        name,
        event_kind,
        price_paise,
        currency,
        per_user_limit,
        hold_ttl_seconds,
        len(labels),
        ShowStatus.ON_SALE.value,
        current_request_id(),
    )
    # One statement for the whole hall: a loop is a round trip per seat.
    await conn.execute(
        """
        INSERT INTO seats (id, show_id, label, status, request_id)
        SELECT seat.id, $1, seat.label, $4, $5
          FROM unnest($2::uuid[], $3::text[]) AS seat (id, label)
        """,
        row["id"],
        [uuid4() for _ in labels],
        labels,
        SeatStatus.AVAILABLE.value,
        current_request_id(),
    )
    return _show(row)


async def get_show(conn: asyncpg.Connection, show_id: UUID) -> Show | None:
    row = await conn.fetchrow(f"SELECT {_SHOW_COLUMNS} FROM shows WHERE id = $1", show_id)
    return _show(row) if row else None


async def list_seats(conn: asyncpg.Connection, show: Show) -> list[SeatView]:
    """Every seat of the show from one statement, so one snapshot. Counts are derived
    from these same rows, which is why they cannot disagree with the seat list or fail
    to sum to the total."""
    rows = await conn.fetch(
        f"""
        SELECT label, section,
               COALESCE(price_paise, $2::bigint) AS price_paise,
               {SEAT_EFFECTIVE_STATUS} AS status,
               CASE WHEN status = 'held' AND hold_expires_at > now()
                    THEN hold_expires_at END AS held_until
          FROM seats
         WHERE show_id = $1
         ORDER BY label
        """,
        show.id,
        show.price_paise,
    )
    return [
        SeatView(
            label=row["label"],
            status=SeatStatus(row["status"]),
            price_paise=row["price_paise"],
            section=row["section"],
            held_until=row["held_until"],
        )
        for row in rows
    ]


async def available_by_show(conn: asyncpg.Connection, max_shows: int) -> dict[UUID, int]:
    """Available seats for the most recent shows, by the same predicate the claim uses,
    so the gauge reports what a claim would actually find."""
    rows = await conn.fetch(
        f"""
        SELECT show_id, count(*) FILTER (WHERE {SEAT_CLAIMABLE}) AS available
          FROM seats
         WHERE show_id IN (SELECT id FROM shows ORDER BY created_at DESC LIMIT $1)
         GROUP BY show_id
        """,
        max_shows,
    )
    return {row["show_id"]: row["available"] for row in rows}
