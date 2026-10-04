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


async def test_seat_overrides_set_price_and_section_and_the_reserve_charges_them(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    new_guest: Callable[[], Any],
) -> None:
    created = await client.post(
        "/shows",
        headers=admin_headers,
        json=BODY | {"seat_overrides": {"A12": {"price_paise": 40001, "section": "premium"}}},
    )
    unknown = await client.post(
        "/shows", headers=admin_headers, json=BODY | {"seat_overrides": {"Z9": {"price_paise": 1}}}
    )
    _, guest = await new_guest()

    assert created.status_code == 201
    seats = {seat["label"]: seat for seat in created.json()["seats"]}
    assert seats["A12"] == {
        "label": "A12",
        "status": "available",
        "price_paise": 40001,
        "section": "premium",
    }
    assert seats["A1"]["price_paise"] == 25000
    assert unknown.status_code == 422

    reserved = await client.post(
        f"/shows/{created.json()['show_id']}/reserve",
        headers=guest | {"Idempotency-Key": "tiered"},
        json={"seats": ["A1", "A12"]},
    )
    # Exact integer sum of the price each seat carried at claim time.
    assert reserved.json()["amount_paise"] == 25000 + 40001


async def test_the_show_list_is_keyset_paginated_newest_first(
    client: httpx.AsyncClient, new_show: Callable[..., Any]
) -> None:
    created = [(await new_show(["A1"]))["show_id"] for _ in range(3)]

    first = (await client.get("/shows", params={"limit": 2})).json()
    second = (
        await client.get("/shows", params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()
    bad_cursor = await client.get("/shows", params={"cursor": "not-a-cursor"})
    too_many = await client.get("/shows", params={"limit": 100000})

    assert [item["show_id"] for item in first["items"]] == created[:0:-1]
    assert second["items"][0]["show_id"] == created[0]
    # A catalogue row carries no seat counts: the list never scans seats.
    assert "counts" not in first["items"][0]
    assert "seats" not in first["items"][0]
    assert bad_cursor.status_code == too_many.status_code == 422


async def test_an_admin_deletes_a_show_and_everything_booked_on_it(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    new_show: Callable[..., Any],
    new_guest: Callable[[], Any],
) -> None:
    show = await new_show(["A1", "A2", "A3"])
    url = f"/shows/{show['show_id']}"
    _, guest = await new_guest()
    reserve_url = f"{url}/reserve"
    booked = await client.post(
        reserve_url, headers=guest | {"Idempotency-Key": "before-delete"}, json={"seats": ["A1"]}
    )
    held = await client.post(
        reserve_url,
        headers=guest | {"Idempotency-Key": "held-before-delete"},
        json={"seats": ["A2"], "hold_ttl_seconds": 60},
    )
    assert (booked.status_code, held.status_code) == (201, 201)

    as_guest = await client.delete(url, headers=guest)
    anonymous = await client.delete(url)
    deleted = await client.delete(url, headers=admin_headers)
    again = await client.delete(url, headers=admin_headers)

    assert (as_guest.status_code, anonymous.status_code) == (403, 401)
    assert deleted.status_code == 200
    assert deleted.json() == {
        "show_id": show["show_id"],
        "deleted": {"reservations": 2, "seats": 3},
    }
    assert again.status_code == 404
    assert (await client.get(url)).json()["error"]["code"] == "SHOW_NOT_FOUND"
    gone = await client.get(f"/reservations/{booked.json()['reservation_id']}", headers=guest)
    assert gone.json()["error"]["code"] == "RESERVATION_NOT_FOUND"
    # The same key is not replayed as a booking that no longer exists.
    retry = await client.post(
        reserve_url, headers=guest | {"Idempotency-Key": "before-delete"}, json={"seats": ["A1"]}
    )
    assert (retry.status_code, retry.json()["error"]["code"]) == (404, "SHOW_NOT_FOUND")
    mine = await client.get("/reservations", headers=guest)
    assert mine.json()["items"] == []


async def test_a_show_carries_its_id_under_the_brief_s_name_and_a_big_hall_is_accepted(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    """The brief says a created show is returned "with an id"; and a hall is N seats,
    with no small ceiling on N."""
    seats = [f"R{row:03d}-{seat:02d}" for row in range(150) for seat in range(40)]

    created = await client.post(
        "/shows", headers=admin_headers, json={"name": "arena", "seats": seats, "price_paise": 1}
    )

    assert created.status_code == 201
    show = created.json()
    assert show["id"] == show["show_id"]
    assert show["total_seats"] == len(seats) == 6000
    assert show["counts"] == {"available": 6000, "held": 0, "confirmed": 0, "total": 6000}
    assert (await client.delete(f"/shows/{show['id']}", headers=admin_headers)).status_code == 200
