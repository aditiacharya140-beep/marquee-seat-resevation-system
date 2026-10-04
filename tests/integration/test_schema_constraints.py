"""The constraints the correctness argument leans on, fired directly (SEAT-008/019/024).

Raw SQL on purpose: these tests assert what the database refuses, which is exactly
what the repository layer exists to never attempt.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest

from app.db.session import acquire


async def test_a_half_upgraded_guest_is_unrepresentable(db: None) -> None:
    async with acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError, match="ck_users_creds"):
            await conn.execute(
                "INSERT INTO users (id, email, role, is_guest) VALUES ($1, $2, 'user', true)",
                uuid4(),
                f"{uuid4().hex}@example.com",
            )


async def _show_with_one_seat(conn: asyncpg.Connection) -> tuple[object, object, object]:
    show_id, seat_id, user_id = uuid4(), uuid4(), uuid4()
    await conn.execute("INSERT INTO users (id, role, is_guest) VALUES ($1, 'user', true)", user_id)
    await conn.execute(
        """INSERT INTO shows (id, event_kind, name, price_paise, currency, per_user_limit,
                              hold_ttl_seconds, total_seats, status)
           VALUES ($1, 'cinema', 'constraints', 100, 'INR', 4, 60, 1, 'on_sale')""",
        show_id,
    )
    await conn.execute(
        "INSERT INTO seats (id, show_id, label) VALUES ($1, $2, 'A1')", seat_id, show_id
    )
    return show_id, seat_id, user_id


async def test_an_incoherent_seat_state_is_rejected(db: None) -> None:
    async with acquire() as conn:
        show_id, seat_id, _ = await _show_with_one_seat(conn)

        with pytest.raises(asyncpg.CheckViolationError, match="ck_seats_hold_coherent"):
            await conn.execute("UPDATE seats SET status = 'held' WHERE id = $1", seat_id)
        with pytest.raises(asyncpg.CheckViolationError, match="ck_seats_hold_coherent"):
            await conn.execute("UPDATE seats SET hold_expires_at = now() WHERE id = $1", seat_id)
        with pytest.raises(asyncpg.UniqueViolationError, match="uq_seats_show_label"):
            await conn.execute(
                "INSERT INTO seats (id, show_id, label) VALUES ($1, $2, 'A1')", uuid4(), show_id
            )


async def test_the_backstop_rejects_a_second_active_claim(db: None) -> None:
    async with acquire() as conn:
        show_id, seat_id, user_id = await _show_with_one_seat(conn)
        reservations = [uuid4(), uuid4()]
        for reservation_id in reservations:
            await conn.execute(
                """INSERT INTO reservations (id, show_id, user_id, status, seat_count,
                                             amount_paise, currency)
                   VALUES ($1, $2, $3, 'confirmed', 1, 100, 'INR')""",
                reservation_id,
                show_id,
                user_id,
            )
        claim = """INSERT INTO reservation_seats (reservation_id, seat_id, label, price_paise)
                   VALUES ($1, $2, 'A1', 100)"""
        await conn.execute(claim, reservations[0], seat_id)

        with pytest.raises(asyncpg.UniqueViolationError, match="uq_seat_active_claim"):
            await conn.execute(claim, reservations[1], seat_id)

        # A released claim no longer blocks the next one.
        await conn.execute(
            "UPDATE reservation_seats SET released_at = now() WHERE seat_id = $1", seat_id
        )
        await conn.execute(claim, reservations[1], seat_id)


async def test_the_quota_table_stores_no_tally(db: None) -> None:
    async with acquire() as conn:
        columns = await conn.fetch(
            """SELECT column_name FROM information_schema.columns
                WHERE table_name = 'user_show_quota'"""
        )

    assert {row["column_name"] for row in columns} == {"user_id", "show_id", "created_at"}
