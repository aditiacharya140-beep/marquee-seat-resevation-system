"""Alembic environment. Online only, over the same DSN and driver the service uses."""

import asyncio

import asyncpg
from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.logging import configure_logging


def _run(connection: Connection) -> None:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _migrate() -> None:
    # async_creator hands asyncpg the DSN untouched, so a connection string that works
    # for the pool works here; SQLAlchemy's URL parser never sees it.
    engine = create_async_engine(
        "postgresql+asyncpg://",
        poolclass=pool.NullPool,
        async_creator=lambda: asyncpg.connect(settings.database_url),
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


configure_logging()
asyncio.run(_migrate())
