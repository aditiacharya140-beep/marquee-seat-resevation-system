"""Cancel and confirm of a hold (REQ-030..REQ-034, ADR-022)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from tests.conftest import error_code, reserve


async def test_cancel_releases_the_seats_and_they_are_rebookable(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1", "A2"])
    _, holder = await new_guest()
    _, other = await new_guest()
    held = (
        await reserve(client, show["show_id"], holder, ["A1", "A2"], hold_ttl_seconds=60)
    ).json()
    url = f"/reservations/{held['reservation_id']}/cancel"

    by_other = await client.post(url, headers=other)
    cancelled = await client.post(url, headers=holder)
    repeated = await client.post(url, headers=holder)
    rebooked = await reserve(client, show["show_id"], other, ["A1", "A2"])

    # 404, not 403: a non-owner must not learn the reservation exists.
    assert (by_other.status_code, error_code(by_other)) == (404, "RESERVATION_NOT_FOUND")
    assert cancelled.status_code == repeated.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["seats"] == ["A1", "A2"]
    assert repeated.json() == cancelled.json()
    assert rebooked.status_code == 201
    # A repeat cancel after the seats moved on must not take them from the new owner.
    assert (await client.post(url, headers=holder)).status_code == 200
    counts = (await client.get(f"/shows/{show['show_id']}")).json()["counts"]
    assert counts == {"available": 0, "held": 0, "confirmed": 2, "total": 2}


async def test_confirm_promotes_a_hold(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1"])
    _, holder = await new_guest()
    held = (await reserve(client, show["show_id"], holder, ["A1"], hold_ttl_seconds=60)).json()
    base = f"/reservations/{held['reservation_id']}"

    confirmed = await client.post(f"{base}/confirm", headers=holder)
    repeated = await client.post(f"{base}/confirm", headers=holder)
    fetched = await client.get(base, headers=holder)

    assert confirmed.status_code == repeated.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert "expires_at" not in confirmed.json()
    assert fetched.json() == confirmed.json()
    counts = (await client.get(f"/shows/{show['show_id']}")).json()["counts"]
    assert counts == {"available": 0, "held": 0, "confirmed": 1, "total": 1}


async def test_a_confirmed_booking_can_be_cancelled_by_its_owner_only_and_rebooked(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    """The default reserve confirms outright, so this is the cancel a client actually
    makes: book, cancel, and the seat is cleanly available to someone else."""
    show = await new_show(["A1", "A2"], per_user_limit=2)
    _, owner = await new_guest()
    _, other = await new_guest()
    booked = (await reserve(client, show["show_id"], owner, ["A1", "A2"])).json()
    url = f"/reservations/{booked['reservation_id']}/cancel"
    assert booked["status"] == "confirmed"

    by_other = await client.post(url, headers=other)
    cancelled = await client.post(url, headers=owner)
    after_cancel = (await client.get(f"/shows/{show['show_id']}")).json()["counts"]
    rebooked = await reserve(client, show["show_id"], other, ["A1", "A2"])
    repeated = await client.post(url, headers=owner)
    confirm_after_cancel = await client.post(
        f"/reservations/{booked['reservation_id']}/confirm", headers=owner
    )
    # The cancel freed the owner's allowance, so the limit is not what declines this.
    owner_again = await reserve(client, show["show_id"], owner, ["A1"])

    assert (by_other.status_code, error_code(by_other)) == (404, "RESERVATION_NOT_FOUND")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert after_cancel == {"available": 2, "held": 0, "confirmed": 0, "total": 2}
    assert rebooked.status_code == 201
    # A repeat cancel is idempotent and must not take the seats from their new owner.
    assert repeated.status_code == 200
    assert (confirm_after_cancel.status_code, error_code(confirm_after_cancel)) == (
        409,
        "RESERVATION_CANCELLED",
    )
    assert (owner_again.status_code, error_code(owner_again)) == (409, "SEAT_TAKEN")
    final = (await client.get(f"/shows/{show['show_id']}")).json()["counts"]
    assert final == {"available": 0, "held": 0, "confirmed": 2, "total": 2}


async def test_a_principal_lists_only_their_own_reservations(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1", "A2", "A3", "A4"])
    _, mine = await new_guest()
    _, theirs = await new_guest()
    made = [
        (await reserve(client, show["show_id"], mine, [seat])).json()["reservation_id"]
        for seat in ("A1", "A2", "A3")
    ]
    await reserve(client, show["show_id"], theirs, ["A4"])

    first = (await client.get("/reservations", headers=mine, params={"limit": 2})).json()
    second = (
        await client.get(
            "/reservations", headers=mine, params={"limit": 2, "cursor": first["next_cursor"]}
        )
    ).json()
    held = (await client.get("/reservations", headers=mine, params={"status": "held"})).json()
    anonymous = await client.get("/reservations")

    listed = [item["reservation_id"] for item in first["items"] + second["items"]]
    assert listed == made[::-1]
    assert second["next_cursor"] is None
    assert held["items"] == []
    assert anonymous.status_code == 401
