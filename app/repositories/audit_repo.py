"""The audit trail's statements. Written only by the batched writer, never inside a
request's transaction; read only by the admin console, always bounded by a time
window and a row limit so no query can become a scan of the whole table."""

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.domain.models import AuditRecord

_COLUMNS = """id, request_id, occurred_at, method, path, route, status_code, duration_ms,
              user_id, is_guest, outcome_code, show_id, seat_labels, idempotency_key,
              client_ip"""


async def insert_batch(conn: asyncpg.Connection, records: list[AuditRecord]) -> None:
    await conn.executemany(
        """
        INSERT INTO audit_log (request_id, occurred_at, method, path, route, status_code,
                               duration_ms, user_id, is_guest, outcome_code, show_id,
                               seat_labels, idempotency_key, client_ip)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        """,
        [
            (
                record.request_id,
                record.occurred_at,
                record.method,
                record.path,
                record.route,
                record.status_code,
                record.duration_ms,
                record.user_id,
                record.is_guest,
                record.outcome_code,
                record.show_id,
                record.seat_labels,
                record.idempotency_key,
                record.client_ip,
            )
            for record in records
        ],
    )


async def list_recent(
    conn: asyncpg.Connection,
    *,
    since: datetime,
    before_id: int | None,
    status_code: int | None,
    outcome_code: str | None,
    request_id: UUID | None,
    user_id: UUID | None,
    show_id: UUID | None,
    limit: int,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"""
        SELECT {_COLUMNS}
          FROM audit_log
         WHERE occurred_at >= $1
           AND ($2::bigint IS NULL OR id < $2)
           AND ($3::int IS NULL OR status_code = $3)
           AND ($4::text IS NULL OR outcome_code = $4)
           AND ($5::uuid IS NULL OR request_id = $5)
           AND ($6::uuid IS NULL OR user_id = $6)
           AND ($7::uuid IS NULL OR show_id = $7)
         ORDER BY id DESC
         LIMIT $8
        """,
        since,
        before_id,
        status_code,
        outcome_code,
        request_id,
        user_id,
        show_id,
        limit,
    )
    return [dict(row) for row in rows]


async def status_classes(conn: asyncpg.Connection, since: datetime) -> dict[str, int]:
    rows = await conn.fetch(
        """
        SELECT (status_code / 100) AS class, count(*) AS requests
          FROM audit_log WHERE occurred_at >= $1
         GROUP BY 1 ORDER BY 1
        """,
        since,
    )
    return {f"{row['class']}xx": row["requests"] for row in rows}


async def outcomes(conn: asyncpg.Connection, since: datetime, limit: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT outcome_code AS code, count(*) AS requests
          FROM audit_log
         WHERE occurred_at >= $1 AND outcome_code IS NOT NULL
         GROUP BY 1 ORDER BY 2 DESC LIMIT $2
        """,
        since,
        limit,
    )
    return [dict(row) for row in rows]


async def routes(conn: asyncpg.Connection, since: datetime, limit: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT method, route, count(*) AS requests,
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms))::int AS p50_ms,
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::int AS p95_ms,
               count(*) FILTER (WHERE status_code >= 500) AS server_errors
          FROM audit_log WHERE occurred_at >= $1
         GROUP BY 1, 2 ORDER BY 3 DESC LIMIT $2
        """,
        since,
        limit,
    )
    return [dict(row) for row in rows]


async def per_minute(conn: asyncpg.Connection, since: datetime) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT date_trunc('minute', occurred_at) AS minute,
               count(*) AS requests,
               count(*) FILTER (WHERE status_code BETWEEN 400 AND 499) AS declined,
               count(*) FILTER (WHERE status_code >= 500) AS server_errors
          FROM audit_log WHERE occurred_at >= $1
         GROUP BY 1 ORDER BY 1
        """,
        since,
    )
    return [dict(row) for row in rows]
