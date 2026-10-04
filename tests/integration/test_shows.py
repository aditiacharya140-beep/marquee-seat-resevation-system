"""Show creation and the seat map (REQ-010..REQ-014)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

SEATS = ["A2", "A1", "A12"]
BODY = {"name": "friday-night", "seats": SEATS, "price_paise": 25000}


async def test_admin_creates_a_show_and_every_seat_starts_available(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    created = await client.post("/shows", headers=admin_headers, json=BODY)

    assert created.status_code == 201
    show = created.json()
    assert show["status"] == "on_sale"
    assert show["total_seats"] == 3
    assert show["counts"] == {"available": 3, "held": 0, "confirmed": 0, "total": 3}
    assert [seat["label"] for seat in show["seats"]] == sorted(SEATS)
    assert all(
        seat == {"label": seat["label"], "status": "available", "price_paise": 25000}
        for seat in show["seats"]
    )
    assert (show["currency"], show["event_kind"], show["per_user_limit"]) == ("INR", "cinema", 4)

    fetched = await client.get(f"/shows/{show['show_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == show


async def test_only_an_admin_may_create_a_show(
    client: httpx.AsyncClient, new_guest: Callable[[], Any]
) -> None:
    _, guest_headers = await new_guest()

    as_guest = await client.post("/shows", headers=guest_headers, json=BODY)
    anonymous = await client.post("/shows", json=BODY)

    assert (as_guest.status_code, as_guest.json()["error"]["code"]) == (403, "FORBIDDEN")
    assert (anonymous.status_code, anonymous.json()["error"]["code"]) == (401, "UNAUTHENTICATED")


@pytest.mark.parametrize(
    "override",
    [
        {"seats": []},
        {"seats": ["A1", "A1"]},
        {"seats": ["A" * 17]},
        {"price_paise": -1},
        {"price_paise": 12.5},
        {"event_kind": "rodeo"},
        {"per_user_limit": 0},
        {"user_id": str(uuid4())},
    ],
    ids=["empty", "duplicate", "long-label", "negative", "float", "kind", "limit", "unknown-field"],
)
async def test_an_invalid_show_is_rejected(
    client: httpx.AsyncClient, admin_headers: dict[str, str], override: dict[str, Any]
) -> None:
    response = await client.post("/shows", headers=admin_headers, json=BODY | override)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_an_unknown_show_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/shows/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SHOW_NOT_FOUND"
