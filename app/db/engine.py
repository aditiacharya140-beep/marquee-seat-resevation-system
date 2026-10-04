"""The asyncpg pool. Created in the lifespan, closed on shutdown."""

import asyncpg

from app.core.config import settings
from app.core.constants import SERVICE_NAME


class Database:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            timeout=settings.db_acquire_timeout_seconds,
            # Startup parameters rather than SET in an init hook: the pool issues
            # RESET ALL on release, which reverts a SET but returns to these.
            server_settings={
                "application_name": SERVICE_NAME,
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
                "idle_in_transaction_session_timeout": str(settings.db_idle_txn_timeout_ms),
                "timezone": "UTC",
            },
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise asyncpg.InterfaceError("connection pool is not open")
        return self._pool


database = Database()
