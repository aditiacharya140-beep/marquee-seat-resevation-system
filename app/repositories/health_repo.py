import asyncpg


async def ping(conn: asyncpg.Connection) -> None:
    await conn.execute("SELECT 1")
