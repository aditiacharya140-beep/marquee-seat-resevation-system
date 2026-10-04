"""Give a deployment a believable cinema programme, partly sold (mds/18-frontend.md).

    ADMIN_EMAIL=… ADMIN_PASSWORD=… python scripts/seed_demo.py <BASE_URL> [--fill 0.35]

Creates each show below unless one of that name already exists, so it is safe to run
twice. The seats it sells are bought the way a visitor buys them — a guest token and a
reserve — so the page shows a hall that is really part sold, not a mock of one.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import httpx

#: (title, days from today, time, screen)
PROGRAMME = [
    ("Interstellar", 0, "7:30 PM", "Screen 1"),
    ("Dune: Part Two", 0, "9:45 PM", "Screen 2"),
    ("3 Idiots", 1, "4:15 PM", "Screen 1"),
    ("Oppenheimer", 1, "6:00 PM", "Screen 3"),
    ("Spirited Away", 1, "8:30 PM", "Screen 2"),
    ("RRR", 2, "9:00 PM", "Screen 1"),
]

#: (rows, seats per row, section, price in paise), front of the hall first.
HALL = [
    ("AB", 16, "Classic", 18000),
    ("CDEFG", 16, "Prime", 25000),
    ("HJK", 12, "Recliner", 45000),
]
#: The catalogue lists only a show's base price, so it is the cheapest tier.
BASE_PRICE_PAISE = min(price for *_, price in HALL)
MAX_PARTY = 4
CONCURRENCY = 8
THROTTLE_RETRIES = 30


def admin_credentials() -> tuple[str, str]:
    """The environment first, then ./.env, which holds a local target's credentials."""
    declared = {}
    env_file = Path(".env")
    if env_file.exists():
        declared = dict(
            line.split("=", 1)
            for line in env_file.read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
    email = os.environ.get("ADMIN_EMAIL") or declared.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD") or declared.get("ADMIN_PASSWORD")
    if not email or not password:
        sys.exit("set ADMIN_EMAIL and ADMIN_PASSWORD to the deployment's admin credentials")
    return email.strip(), password.strip()


def show_name(title: str, days: int, time: str, screen: str) -> str:
    day = date.today() + timedelta(days=days)
    return f"{title} · {day:%a} {day.day} {day:%b}, {time} · {screen}"


def hall() -> tuple[list[list[str]], dict[str, dict[str, object]]]:
    rows, overrides = [], {}
    for letters, per_row, section, price in HALL:
        for letter in letters:
            labels = [f"{letter}{number}" for number in range(1, per_row + 1)]
            rows.append(labels)
            overrides |= {label: {"price_paise": price, "section": section} for label in labels}
    return rows, overrides


def parties(rows: list[list[str]], fill: float, rng: random.Random) -> list[list[str]]:
    """Groups of adjacent seats, as people actually sit, up to `fill` of the hall."""
    target = int(sum(len(row) for row in rows) * fill)
    free = [list(row) for row in rows]
    chosen: list[list[str]] = []
    while sum(len(party) for party in chosen) < target:
        row = rng.choice([row for row in free if row])
        size = min(rng.randint(1, MAX_PARTY), len(row))
        start = rng.randrange(len(row) - size + 1)
        chosen.append(row[start : start + size])
        del row[start : start + size]
    return chosen


async def post(client: httpx.AsyncClient, path: str, **kwargs: object) -> httpx.Response:
    """A throttled request waits the time the service asks for and tries again."""
    for _ in range(THROTTLE_RETRIES):
        response = await client.post(path, **kwargs)  # type: ignore[arg-type]
        if response.status_code != 429:
            return response
        await asyncio.sleep(int(response.headers.get("Retry-After", "1")))
    return response


async def existing_names(client: httpx.AsyncClient) -> set[str]:
    names: set[str] = set()
    cursor = None
    while True:
        page = (await client.get("/shows", params={"cursor": cursor} if cursor else None)).json()
        names |= {show["name"] for show in page["items"]}
        cursor = page["next_cursor"]
        if not cursor:
            return names


async def sell(
    client: httpx.AsyncClient, show_id: str, party: list[str], gate: asyncio.Semaphore
) -> int:
    async with gate:
        guest = await post(client, "/auth/guest")
        if guest.status_code != 201:
            return 0
        sold = await post(
            client,
            f"/shows/{show_id}/reserve",
            headers={
                "Authorization": f"Bearer {guest.json()['access_token']}",
                "Idempotency-Key": uuid.uuid4().hex,
            },
            json={"seats": party},
        )
        return len(party) if sold.status_code == 201 else 0


async def main(args: argparse.Namespace) -> None:
    email, password = admin_credentials()
    rng = random.Random(args.seed)
    rows, overrides = hall()
    gate = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=60) as client:
        login = await post(client, "/auth/login", json={"email": email, "password": password})
        if login.status_code != 200:
            sys.exit(f"admin login failed: {login.status_code} {login.text}")
        admin = {"Authorization": f"Bearer {login.json()['access_token']}"}
        already = await existing_names(client)

        # Last in the programme first: the catalogue lists newest first.
        for entry in reversed(PROGRAMME):
            name = show_name(*entry)
            if name in already:
                print(f"exists   {name}")
                continue
            created = await post(
                client,
                "/shows",
                headers=admin,
                json={
                    "name": name,
                    "seats": [label for row in rows for label in row],
                    "price_paise": BASE_PRICE_PAISE,
                    "seat_overrides": overrides,
                },
            )
            if created.status_code != 201:
                sys.exit(f"show creation failed: {created.status_code} {created.text}")
            show_id = created.json()["show_id"]
            sold = await asyncio.gather(
                *(sell(client, show_id, party, gate) for party in parties(rows, args.fill, rng))
            )
            print(f"created  {name}: {sum(sold)} of {len(overrides)} seats sold")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("base_url")
    parser.add_argument("--fill", type=float, default=0.35, help="share of each hall to sell")
    parser.add_argument("--seed", type=int, default=7)
    asyncio.run(main(parser.parse_args()))
