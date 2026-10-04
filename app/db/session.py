"""Connection and transaction scopes. Services own these; repositories never do."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.errors import DependencyError
from app.db.engine import database

#: What "the database is not there" looks like from the driver. A statement timeout is
#: in this set and a lock timeout is not: the first is a fault, the second a decline
#: the repository translates (ADR-027).
_UNAVAILABLE = (
    TimeoutError,
    OSError,
    asyncpg.InterfaceError,
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
    asyncpg.QueryCanceledError,
)


@asynccontextmanager
async def acquire(wait_seconds: float | None = None) -> AsyncIterator[asyncpg.Connection]:
    try:
        if wait_seconds is None:
            wait_seconds = settings.db_acquire_timeout_seconds
        async with database.pool.acquire(timeout=wait_seconds) as connection:
            yield connection
    except _UNAVAILABLE as exc:
        raise DependencyError(ErrorCode.DATABASE_UNAVAILABLE) from exc


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    async with acquire() as connection, connection.transaction():
        yield connection
