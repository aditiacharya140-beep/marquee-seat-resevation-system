"""`/metrics` agrees with the API (REQ-042, REQ-043)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx

from tests.conftest import reserve


def value(exposition: str, series: str) -> float:
    match = re.search(rf"^{re.escape(series)} (\S+)$", exposition, re.MULTILINE)
    return float(match.group(1)) if match else 0.0


async def test_counters_and_the_gauge_reconcile_with_api_state(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1", "A2", "A3"])
    show_id = show["show_id"]
    _, first = await new_guest()
    _, second = await new_guest()
    gauge = f'seats_available{{show_id="{show_id}"}}'
    taken = 'reservations_declined_total{reason="seat_taken"}'
    replay = 'reservations_declined_total{reason="idempotent_replay"}'
    before = (await client.get("/metrics")).text

    await reserve(client, show_id, first, ["A1"], key="k")
    await reserve(client, show_id, first, ["A1"], key="k")
    await reserve(client, show_id, second, ["A1"])
    after = await client.get("/metrics")

    assert after.status_code == 200
    assert after.headers["content-type"].startswith("text/plain")
    text = after.text
    assert value(before, gauge) == 3
    assert value(text, gauge) == 2
    counts = (await client.get(f"/shows/{show_id}")).json()["counts"]
    assert value(text, gauge) == counts["available"]
    confirmed = "reservations_confirmed_total"
    assert value(text, confirmed) - value(before, confirmed) == 1
    assert value(text, taken) - value(before, taken) == 1
    assert value(text, replay) - value(before, replay) == 1
    # Bounded labels only: no principal and no seat label ever becomes a series.
    assert "user_id" not in text
    assert 'A1"' not in text
