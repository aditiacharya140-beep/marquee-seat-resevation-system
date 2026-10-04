"""Delete the shows the load test leaves behind, and everything booked on them.

    python scripts/delete_burst_shows.py [DATABASE_URL] [--yes]

The API cannot close or delete a show, so this talks to the database: `DATABASE_URL`
from the argument, the environment, or ./.env, in that order. For the live service that
is the database's *external* URL from the Render dashboard.

One transaction. Reservations are deleted before their show because that foreign key
does not cascade; seats and per-user quota rows go with the show. The guests the burst
minted are left: nothing marks them apart from any other guest.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

#: The name burst/burst.py gives every show it creates.
PATTERN = "burst-%"


def database_url(argument: str | None) -> str:
    if argument:
        return argument
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    sys.exit("pass a DATABASE_URL, or set it in the environment or ./.env")


async def main(args: argparse.Namespace) -> None:
    url = database_url(args.database_url)
    target = urlsplit(url)
    connection = await asyncpg.connect(url)
    try:
        shows = await connection.fetch(
            """
            SELECT s.name, s.total_seats,
                   (SELECT count(*) FROM reservations r WHERE r.show_id = s.id) AS reservations
              FROM shows s WHERE s.name LIKE $1 ORDER BY s.created_at
            """,
            PATTERN,
        )
        print(f"database {target.path.lstrip('/')} on {target.hostname}")
        if not shows:
            print("no burst shows: nothing to delete")
            return
        for show in shows:
            booked = f"{show['total_seats']} seats, {show['reservations']} reservations"
            print(f"  {show['name']}: {booked}")
        if not args.yes and input(f"delete {len(shows)} show(s)? [y/N] ").lower() != "y":
            sys.exit("nothing deleted")

        async with connection.transaction():
            await connection.execute(
                """
                CREATE TEMP TABLE doomed ON COMMIT DROP AS
                SELECT r.id FROM reservations r JOIN shows s ON s.id = r.show_id
                 WHERE s.name LIKE $1
                """,
                PATTERN,
            )
            for table, column in (
                ("reservation_seats", "reservation_id"),
                ("idempotency_keys", "reservation_id"),
                ("reservations", "id"),
            ):
                done = await connection.execute(
                    f"DELETE FROM {table} WHERE {column} IN (SELECT id FROM doomed)"
                )
                print(f"  {table}: {done.split()[-1]} rows")
            done = await connection.execute("DELETE FROM shows WHERE name LIKE $1", PATTERN)
            print(f"  shows: {done.split()[-1]} rows, with their seats")
    finally:
        await connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("database_url", nargs="?")
    parser.add_argument("--yes", action="store_true", help="do not ask before deleting")
    asyncio.run(main(parser.parse_args()))
