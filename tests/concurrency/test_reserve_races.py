"""One test per invariant, each against real Postgres with real parallelism.

Every request runs on its own pooled connection, so the database arbitrates exactly
as it would between separate clients. A test here that would still pass against a
read-then-write claim is not testing anything: the hot-seat test was checked against
a claim predicate replaced with `true`, and fails.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core.metrics import unhandled_exceptions_total
from tests.conftest import error_code, reserve

pytestmark = pytest.mark.concurrency

CONTENDERS = 60


def outcomes(responses: list[httpx.Response]) -> Counter[Any]:
    return Counter((r.status_code, error_code(r)) for r in responses)


def unhandled_total() -> float:
    return sum(
        sample.value
        for metric in unhandled_exceptions_total.collect()
        for sample in metric.samples
        if sample.name.endswith("_total")
    )


async def guests(new_guest: Callable[[], Any], count: int) -> list[dict[str, str]]:
    return [headers for _, headers in await asyncio.gather(*(new_guest() for _ in range(count)))]


async def counts(client: httpx.AsyncClient, show_id: str) -> dict[str, int]:
    return dict((await client.get(f"/shows/{show_id}")).json()["counts"])


async def test_hot_seat_has_exactly_one_winner_and_no_5xx(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A12"])
    principals = await guests(new_guest, CONTENDERS)
    faults_before = unhandled_total()

    responses = await asyncio.gather(
        *(reserve(client, show["show_id"], headers, ["A12"]) for headers in principals)
    )

    assert outcomes(responses) == {(201, None): 1, (409, "SEAT_TAKEN"): CONTENDERS - 1}
    assert unhandled_total() == faults_before
    assert await counts(client, show["show_id"]) == {
        "available": 0,
        "held": 0,
        "confirmed": 1,
        "total": 1,
    }


async def test_one_principal_never_exceeds_the_limit(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    limit, attempts = 4, 12
    seats = [f"S{i:02d}" for i in range(attempts)]
    show = await new_show(seats, per_user_limit=limit)
    _, headers = await new_guest()

    responses = await asyncio.gather(
        *(reserve(client, show["show_id"], headers, [seat]) for seat in seats)
    )

    assert outcomes(responses) == {(201, None): limit, (409, "PER_USER_LIMIT"): attempts - limit}
    assert (await counts(client, show["show_id"]))["confirmed"] == limit


async def test_one_key_fired_concurrently_reserves_once(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    duplicates = 20
    show = await new_show(["A1", "A2"])
    _, headers = await new_guest()

    responses = await asyncio.gather(
        *(
            reserve(client, show["show_id"], headers, ["A1", "A2"], key="same-key")
            for _ in range(duplicates)
        )
    )

    assert outcomes(responses) == {(201, None): 1, (200, None): duplicates - 1}
    assert all(r.json() == responses[0].json() for r in responses)
    assert all(
        r.headers.get("Idempotent-Replay") == "true" for r in responses if r.status_code == 200
    )
    assert (await counts(client, show["show_id"]))["confirmed"] == 2


async def test_overlapping_multi_seat_claims_in_opposite_orders_do_not_deadlock(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    """Requested orders differ; the lock order does not, which is the whole argument."""
    seats = ["A1", "A2", "A3", "A4"]
    show = await new_show(seats)
    principals = await guests(new_guest, 40)

    responses = await asyncio.gather(
        *(
            reserve(client, show["show_id"], headers, seats if i % 2 else seats[::-1])
            for i, headers in enumerate(principals)
        )
    )

    assert outcomes(responses) == {(201, None): 1, (409, "SEAT_TAKEN"): 39}
    assert (await counts(client, show["show_id"]))["confirmed"] == 4


async def test_reconciliation_holds_while_a_burst_is_in_flight(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    seats = [f"R{i:02d}" for i in range(20)]
    show = await new_show(seats)
    principals = await guests(new_guest, 80)
    rng = random.Random(7)
    samples: list[dict[str, int]] = []
    burst_done = asyncio.Event()

    async def sample() -> None:
        while not burst_done.is_set():
            samples.append(await counts(client, show["show_id"]))

    sampler = asyncio.create_task(sample())
    responses = await asyncio.gather(
        *(
            reserve(client, show["show_id"], headers, rng.sample(seats, rng.randint(1, 3)))
            for headers in principals
        )
    )
    burst_done.set()
    await sampler
    final = await counts(client, show["show_id"])

    assert samples
    for snapshot in [*samples, final]:
        assert snapshot["available"] + snapshot["held"] + snapshot["confirmed"] == 20
    assert all(r.status_code in (201, 409) for r in responses), outcomes(responses)
    seats_sold = sum(len(r.json()["seats"]) for r in responses if r.status_code == 201)
    assert final["confirmed"] == seats_sold
    sold_labels = [label for r in responses if r.status_code == 201 for label in r.json()["seats"]]
    assert len(sold_labels) == len(set(sold_labels)), "a seat was sold twice"


async def test_a_lapsed_hold_is_claimable_with_no_sweeper(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    """Mechanism 3: the lapsed hold's claim row is still active when the next claim
    arrives, and must be closed rather than tripping the backstop index."""
    show = await new_show(["A1"])
    _, holder = await new_guest()
    principals = await guests(new_guest, 10)
    held = await reserve(client, show["show_id"], holder, ["A1"], hold_ttl_seconds=1)
    assert held.json()["status"] == "held"

    await asyncio.sleep(1.2)
    assert (await counts(client, show["show_id"]))["available"] == 1
    responses = await asyncio.gather(
        *(reserve(client, show["show_id"], headers, ["A1"]) for headers in principals)
    )
    late = await client.post(
        f"/reservations/{held.json()['reservation_id']}/confirm", headers=holder
    )

    assert outcomes(responses) == {(201, None): 1, (409, "SEAT_TAKEN"): 9}
    assert (late.status_code, error_code(late)) == (409, "RESERVATION_EXPIRED")
    assert (await counts(client, show["show_id"]))["confirmed"] == 1
