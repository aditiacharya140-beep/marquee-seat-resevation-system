"""The audit write path, built so that it can never slow a booking (REQ-046).

    request ──► AuditMiddleware ──► bounded buffer ──► writer task ──► audit_log
                 append or drop                         batched, own connection

- The request path appends to an in-memory buffer or, if it is full, drops the
  record and counts the drop. It never awaits. Losing an audit row is an
  inconvenience; stalling a booking behind one is a defect.
- The writer drains in batches on a connection of its own, outside the request
  pool, so a saturated pool cannot stall audit and a slow flush cannot starve
  requests.
- Shutdown drains what is left, within a bound.
"""

import asyncio
import contextlib
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from app.core import metrics
from app.core.config import settings
from app.core.constants import LogEvent
from app.core.logging import get_logger
from app.db.session import acquire
from app.domain.models import AuditRecord
from app.repositories import audit_repo

logger = get_logger(__name__)

_buffer: deque[AuditRecord] = deque()
_writer: asyncio.Task[None] | None = None
_connection: asyncpg.Connection | None = None


def enqueue(record: AuditRecord) -> None:
    """Called on the request path. Constant time, never awaits, never raises."""
    if len(_buffer) >= settings.audit_queue_max:
        metrics.audit_records_dropped_total.inc()
        return
    _buffer.append(record)


def queue_depth() -> int:
    return len(_buffer)


def _take_batch() -> list[AuditRecord]:
    size = min(len(_buffer), settings.audit_batch_size)
    return [_buffer.popleft() for _ in range(size)]


async def flush(conn: asyncpg.Connection | None = None) -> int:
    """Write everything buffered. With no connection given, borrows one from the pool —
    which is what tests do; the writer always passes its own."""
    written = 0
    while batch := _take_batch():
        try:
            if conn is None:
                async with acquire() as pooled:
                    await audit_repo.insert_batch(pooled, batch)
            else:
                await audit_repo.insert_batch(conn, batch)
        except Exception as exc:  # noqa: BLE001 - audit must never take the process down
            metrics.audit_records_dropped_total.inc(len(batch))
            logger.error(LogEvent.AUDIT_FLUSH_FAILED, exc_info=exc, extra={"lost": len(batch)})
            raise
        written += len(batch)
        metrics.audit_records_written_total.inc(len(batch))
    return written


async def _dedicated_connection() -> asyncpg.Connection:
    global _connection  # noqa: PLW0603 - one writer connection per process
    if _connection is None or _connection.is_closed():
        _connection = await asyncpg.connect(settings.database_url)
    return _connection


async def _run() -> None:
    interval = settings.audit_flush_interval_ms / 1000
    while True:
        await asyncio.sleep(interval)
        if not _buffer:
            continue
        # A failed flush is logged and counted where it fails; the loop must survive it.
        with contextlib.suppress(Exception):
            await flush(await _dedicated_connection())


def start() -> None:
    global _writer  # noqa: PLW0603
    if settings.audit_enabled and _writer is None:
        _writer = asyncio.create_task(_run(), name="audit-writer")


async def stop() -> None:
    """Stop the loop, then drain what is buffered within a bound, so a graceful deploy
    does not lose the last batch."""
    global _writer, _connection  # noqa: PLW0603
    if _writer is not None:
        _writer.cancel()
        await asyncio.gather(_writer, return_exceptions=True)
        _writer = None
    try:
        async with asyncio.timeout(settings.audit_shutdown_flush_seconds):
            await flush(await _dedicated_connection())
    except Exception as exc:  # noqa: BLE001 - shutting down regardless
        logger.warning(
            LogEvent.AUDIT_SHUTDOWN_FLUSH_INCOMPLETE, extra={"reason": type(exc).__name__}
        )
    if _connection is not None and not _connection.is_closed():
        await _connection.close()
    _connection = None


def _since(window_minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=window_minutes)


async def recent(
    *,
    window_minutes: int,
    before_id: int | None,
    status_code: int | None,
    outcome_code: str | None,
    request_id: UUID | None,
    user_id: UUID | None,
    show_id: UUID | None,
    limit: int,
) -> list[dict[str, Any]]:
    async with acquire() as conn:
        return await audit_repo.list_recent(
            conn,
            since=_since(window_minutes),
            before_id=before_id,
            status_code=status_code,
            outcome_code=outcome_code,
            request_id=request_id,
            user_id=user_id,
            show_id=show_id,
            limit=limit,
        )


async def summary(window_minutes: int) -> dict[str, Any]:
    since = _since(window_minutes)
    async with acquire() as conn:
        classes = await audit_repo.status_classes(conn, since)
        return {
            "total": sum(classes.values()),
            "by_status_class": classes,
            "by_outcome": await audit_repo.outcomes(conn, since, settings.admin_top_n),
            "by_route": await audit_repo.routes(conn, since, settings.admin_top_n),
            "per_minute": await audit_repo.per_minute(conn, since),
        }
