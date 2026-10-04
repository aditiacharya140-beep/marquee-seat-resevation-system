"""The claim. Every statement here is quoted in mds/04-concurrency-and-atomicity.md;
a change to one is a change to the other.

Lock order for a claiming transaction, which is the whole of the deadlock argument:
its own idempotency key row, then the principal's quota row, then seat rows in
ascending label order. Nothing in this module may take a quota lock after a seat lock.
"""

from uuid import UUID

import asyncpg

from app.core.constants import DeclineReason, LogEvent, LogLevel
from app.core.error_codes import ErrorCode
from app.core.errors import AuthError, ConflictError
from app.core.logging import get_logger
from app.db.sql import SEAT_ACTIVE, SEAT_CLAIMABLE
from app.domain.models import ClaimedSeat
from app.repositories.base import contention_is_a_decline, current_request_id

logger = get_logger(__name__)


async def lock_quota(conn: asyncpg.Connection, user_id: UUID, show_id: UUID) -> None:
    """Serialize this principal's concurrent reserves for this show. Always the FIRST
    lock a claim takes. The row is a lock target, never a tally."""
    with contention_is_a_decline():
        try:
            await conn.execute(
                """
                INSERT INTO user_show_quota (user_id, show_id) VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                user_id,
                show_id,
            )
        except asyncpg.ForeignKeyViolationError:
            # A validly signed token whose subject has no row.
            raise AuthError(ErrorCode.UNAUTHENTICATED) from None
        await conn.execute(
            "SELECT 1 FROM user_show_quota WHERE user_id = $1 AND show_id = $2 FOR UPDATE",
            user_id,
            show_id,
        )


async def count_active_for_user(conn: asyncpg.Connection, show_id: UUID, user_id: UUID) -> int:
    """Derived from the seats themselves, so it cannot drift. Only meaningful while the
    caller holds the quota lock."""
    count: int = await conn.fetchval(
        f"SELECT count(*) FROM seats WHERE show_id = $1 AND held_by = $2 AND {SEAT_ACTIVE}",
        show_id,
        user_id,
    )
    return count


async def claim_many(
    conn: asyncpg.Connection,
    *,
    show_id: UUID,
    labels: list[str],
    user_id: UUID,
    reservation_id: UUID,
    ttl_or_none: int | None,
    show_price_paise: int,
) -> list[ClaimedSeat]:
    """The atomic decision: the rows returned are the seats this transaction now owns.

    Fewer rows than labels means a seat was active for someone else; the caller rolls
    back and nothing stays claimed. `ttl_or_none` alone selects the target state —
    `None` confirms outright, a value holds — so status and expiry cannot disagree.

    `ORDER BY label` under `FOR UPDATE` is the deadlock prevention, not a sort: every
    claim locks seats in the same order, so no wait-for cycle is constructible.
    Under READ COMMITTED a blocked row is re-checked against the committed version
    once its lock is granted, so a loser's predicate fails and the row drops out.
    """
    with contention_is_a_decline():
        rows = await conn.fetch(
            f"""
            WITH candidate AS (
                SELECT id, label, COALESCE(price_paise, $7::bigint) AS price_paise
                  FROM seats
                 WHERE show_id = $1
                   AND label = ANY($2::text[])
                   AND {SEAT_CLAIMABLE}
                 ORDER BY label
                   FOR UPDATE
            ),
            claimed AS (
                UPDATE seats s
                   SET status = CASE WHEN $5::int IS NULL THEN 'confirmed' ELSE 'held' END,
                       held_by = $3,
                       reservation_id = $4,
                       hold_expires_at = CASE WHEN $5::int IS NULL THEN NULL
                                              ELSE now() + ($5::int * INTERVAL '1 second') END,
                       version = s.version + 1,
                       updated_at = now(),
                       request_id = $6
                  FROM candidate c
                 WHERE s.id = c.id
                RETURNING s.id, s.label, c.price_paise
            )
            SELECT id, label, price_paise FROM claimed ORDER BY label
            """,
            show_id,
            labels,
            user_id,
            reservation_id,
            ttl_or_none,
            current_request_id(),
            show_price_paise,
        )
    return [ClaimedSeat(id=r["id"], label=r["label"], price_paise=r["price_paise"]) for r in rows]


async def labels_not_in_show(
    conn: asyncpg.Connection, show_id: UUID, labels: list[str]
) -> list[str]:
    """Diagnosis after a shortfall, to tell 404 from 409. Never part of the decision."""
    rows = await conn.fetch(
        """
        SELECT requested.label
          FROM unnest($2::text[]) AS requested (label)
         WHERE NOT EXISTS (
               SELECT 1 FROM seats WHERE show_id = $1 AND label = requested.label)
         ORDER BY requested.label
        """,
        show_id,
        labels,
    )
    return [row["label"] for row in rows]


async def release_for_reservation(conn: asyncpg.Connection, reservation_id: UUID) -> list[str]:
    """Return a hold's seats, guarded on current ownership and on the hold being live,
    so it can never take a seat from whoever claimed it after a lapse (ADR-022)."""
    with contention_is_a_decline():
        rows = await conn.fetch(
            """
            WITH owned AS (
                SELECT id, label FROM seats
                 WHERE reservation_id = $1
                   AND status = 'held' AND hold_expires_at > now()
                 ORDER BY label
                   FOR UPDATE
            ),
            released AS (
                UPDATE seats s
                   SET status = 'available', held_by = NULL, reservation_id = NULL,
                       hold_expires_at = NULL, version = s.version + 1,
                       updated_at = now(), request_id = $2
                  FROM owned o WHERE s.id = o.id
                RETURNING s.label
            )
            SELECT label FROM released ORDER BY label
            """,
            reservation_id,
            current_request_id(),
        )
    return [row["label"] for row in rows]


async def confirm_for_reservation(conn: asyncpg.Connection, reservation_id: UUID) -> list[str]:
    with contention_is_a_decline():
        rows = await conn.fetch(
            """
            WITH owned AS (
                SELECT id, label FROM seats
                 WHERE reservation_id = $1
                   AND status = 'held' AND hold_expires_at > now()
                 ORDER BY label
                   FOR UPDATE
            ),
            promoted AS (
                UPDATE seats s
                   SET status = 'confirmed', hold_expires_at = NULL,
                       version = s.version + 1, updated_at = now(), request_id = $2
                  FROM owned o WHERE s.id = o.id
                RETURNING s.label
            )
            SELECT label FROM promoted ORDER BY label
            """,
            reservation_id,
            current_request_id(),
        )
    return [row["label"] for row in rows]


def backstop_violation() -> ConflictError:
    """`uq_seat_active_claim` fired: the claim predicate let through a seat that had a
    live claim. The client still gets a clean decline; the log line is the alert."""
    logger.error(LogEvent.CLAIM_BACKSTOP_VIOLATED, stack_info=True)
    return ConflictError(
        ErrorCode.SEAT_TAKEN,
        details={"reason": DeclineReason.ACTIVE_CLAIM_BACKSTOP.value},
        log_level=LogLevel.ERROR,
    )
