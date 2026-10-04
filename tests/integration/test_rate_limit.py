"""Per-identity rate limiting (REQ-047, SEAT-052)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.core.config import settings
from app.core.constants import Header
from app.core.metrics import rate_limited_total
from app.middleware.rate_limit import classify, parse_ceiling
from tests.conftest import error_code, reserve


def address() -> dict[str, str]:
    """A distinct client per test: buckets live for the whole test session."""
    octets = uuid4().bytes[:3]
    return {"X-Forwarded-For": f"10.{octets[0]}.{octets[1]}.{octets[2]}"}


@pytest.fixture
def limited(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    return monkeypatch


def _throttled(route_class: str) -> float:
    return sum(
        sample.value
        for metric in rate_limited_total.collect()
        for sample in metric.samples
        if sample.name.endswith("_total") and sample.labels.get("route_class") == route_class
    )


async def test_guest_issuance_is_capped_per_address(
    client: httpx.AsyncClient, limited: pytest.MonkeyPatch
) -> None:
    limited.setattr(settings, "rate_limit_guest", "3/600s")
    mine, other = address(), address()
    before = _throttled("guest")

    responses = [await client.post("/auth/guest", headers=mine) for _ in range(5)]
    elsewhere = await client.post("/auth/guest", headers=other)

    assert [r.status_code for r in responses] == [201, 201, 201, 429, 429]
    refused = responses[-1]
    assert error_code(refused) == "RATE_LIMITED"
    assert refused.json()["error"]["request_id"] == refused.headers[Header.REQUEST_ID]
    assert int(refused.headers["Retry-After"]) >= 1
    assert refused.headers["X-RateLimit-Limit"] == "3"
    assert refused.headers["X-RateLimit-Remaining"] == "0"
    assert int(refused.headers["X-RateLimit-Reset"]) > 0
    assert elsewhere.status_code == 201
    assert _throttled("guest") - before == 2


async def test_a_client_cannot_choose_its_bucket_by_forging_the_header(
    client: httpx.AsyncClient, limited: pytest.MonkeyPatch
) -> None:
    """Whatever a client writes arrives to the left of what the proxy appends."""
    limited.setattr(settings, "rate_limit_guest", "2/600s")
    real = address()["X-Forwarded-For"]

    responses = [
        await client.post("/auth/guest", headers={"X-Forwarded-For": f"203.0.113.{i}, {real}"})
        for i in range(4)
    ]

    assert [r.status_code for r in responses] == [201, 201, 429, 429]


async def test_distinct_principals_behind_one_address_are_never_throttled(
    client: httpx.AsyncClient,
    new_show: Callable[..., Any],
    new_guest: Callable[[], Any],
    limited: pytest.MonkeyPatch,
) -> None:
    """The property that keeps an on-sale rush from becoming a wall of 429s."""
    seats = [f"S{i:02d}" for i in range(12)]
    show = await new_show(seats)
    principals = [headers for _, headers in [await new_guest() for _ in range(12)]]
    limited.setattr(settings, "rate_limit_reserve", "2/600s")
    shared = address()

    crowd = await asyncio.gather(
        *(
            reserve(client, show["show_id"], headers | shared, [seat])
            for headers, seat in zip(principals, seats, strict=True)
        )
    )
    one_principal = [
        await reserve(client, show["show_id"], principals[0] | shared, ["S00"]) for _ in range(3)
    ]

    assert [r.status_code for r in crowd] == [201] * 12
    # That principal already spent one of its two; the second passes, then it is throttled.
    assert [r.status_code for r in one_principal] == [409, 429, 429]


async def test_health_and_metrics_are_exempt_and_readyz_reports_the_switch(
    client: httpx.AsyncClient, limited: pytest.MonkeyPatch
) -> None:
    limited.setattr(settings, "rate_limit_read", "1/600s")
    mine = address()

    statuses = [
        (await client.get(path, headers=mine)).status_code
        for path in ("/healthz", "/readyz", "/metrics") * 3
    ]
    ready = (await client.get("/readyz", headers=mine)).json()
    limited.setattr(settings, "rate_limit_enabled", False)
    unlimited = (await client.get("/readyz", headers=mine)).json()

    assert statuses == [200] * 9
    assert ready["rate_limit_enabled"] is True
    assert unlimited["rate_limit_enabled"] is False


async def test_a_bucket_refills_over_its_window(
    client: httpx.AsyncClient, limited: pytest.MonkeyPatch
) -> None:
    limited.setattr(settings, "rate_limit_guest", "2/1s")
    mine = address()

    first = [(await client.post("/auth/guest", headers=mine)).status_code for _ in range(3)]
    await asyncio.sleep(0.6)
    later = (await client.post("/auth/guest", headers=mine)).status_code

    assert first == [201, 201, 429]
    assert later == 201


def test_route_classes_and_ceiling_parsing() -> None:
    assert classify("GET", "/healthz") is None
    assert classify("POST", "/auth/guest") == "guest"
    assert classify("POST", "/auth/login") == "auth"
    assert classify("GET", "/auth/me") == "read"
    assert classify("POST", "/shows") == "admin"
    assert classify("POST", "/shows/abc/reserve") == "reserve"
    assert classify("POST", "/reservations/abc/cancel") == "reserve"
    assert classify("GET", "/shows/abc") == "read"
    assert (parse_ceiling("120/10s").requests, parse_ceiling("120/10s").seconds) == (120, 10)
    with pytest.raises(ValueError, match="rate limit"):
        parse_ceiling("fast")
