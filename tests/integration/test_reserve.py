"""The reserve contract, one request at a time (REQ-020..REQ-029). The races are in
tests/concurrency/."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx

from tests.conftest import error_code, reserve


async def test_a_reserve_confirms_outright_and_is_owned_by_the_token_subject(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1", "A2", "A3"])
    user_id, headers = await new_guest()

    # A spoofed identity field: ignored, because no request model declares one.
    response = await reserve(client, show["show_id"], headers, ["A2", "A1"], user_id=str(uuid4()))

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == user_id
    assert body["seats"] == ["A1", "A2"]
    assert (body["status"], body["amount_paise"], body["currency"]) == ("confirmed", 50000, "INR")
    assert "confirmed_at" in body
    assert "expires_at" not in body

    after = (await client.get(f"/shows/{show['show_id']}")).json()
    assert after["counts"] == {"available": 1, "held": 0, "confirmed": 2, "total": 3}
    assert "held_by" not in str(after)


async def test_a_reserve_with_a_ttl_holds_and_reports_the_clamped_expiry(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1"], hold_ttl_seconds=60)
    _, headers = await new_guest()

    response = await reserve(client, show["show_id"], headers, ["A1"], hold_ttl_seconds=600)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "held"
    assert "confirmed_at" not in body
    seat = (await client.get(f"/shows/{show['show_id']}")).json()["seats"][0]
    assert seat["status"] == "held"
    assert seat["held_until"] == body["expires_at"]


async def test_a_partly_taken_request_claims_nothing(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1", "A2", "A3"])
    _, first = await new_guest()
    _, second = await new_guest()
    await reserve(client, show["show_id"], first, ["A2"])

    response = await reserve(client, show["show_id"], second, ["A1", "A2", "A3"])

    assert (response.status_code, error_code(response)) == (409, "SEAT_TAKEN")
    assert response.json()["error"]["details"] == {
        "requested": ["A1", "A2", "A3"],
        "conflicts": ["A2"],
    }
    counts = (await client.get(f"/shows/{show['show_id']}")).json()["counts"]
    assert counts == {"available": 2, "held": 0, "confirmed": 1, "total": 3}


async def test_a_decline_releases_the_key_so_a_retry_is_a_real_attempt(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1"], hold_ttl_seconds=60)
    _, holder = await new_guest()
    _, waiter = await new_guest()
    held = await reserve(client, show["show_id"], holder, ["A1"], hold_ttl_seconds=60)

    declined = await reserve(client, show["show_id"], waiter, ["A1"], key="retry-me")
    await client.post(f"/reservations/{held.json()['reservation_id']}/cancel", headers=holder)
    retried = await reserve(client, show["show_id"], waiter, ["A1"], key="retry-me")

    assert (declined.status_code, error_code(declined)) == (409, "SEAT_TAKEN")
    assert retried.status_code == 201
    assert "Idempotent-Replay" not in retried.headers


async def test_a_replay_answers_200_with_the_original_body(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1", "A2"])
    other_show = await new_show(["A1", "A2"])
    _, headers = await new_guest()

    first = await reserve(client, show["show_id"], headers, ["A1"], key="k1")
    replay = await reserve(client, show["show_id"], headers, ["A1"], key="k1")
    via_body = await client.post(
        f"/shows/{show['show_id']}/reserve",
        headers=headers,
        json={"seats": ["A1"], "idempotency_key": "k1"},
    )
    different_seats = await reserve(client, show["show_id"], headers, ["A2"], key="k1")
    different_show = await reserve(client, other_show["show_id"], headers, ["A1"], key="k1")

    assert first.status_code == 201
    for response in (replay, via_body):
        assert response.status_code == 200
        assert response.headers["Idempotent-Replay"] == "true"
        assert response.json() == first.json()
    for response in (different_seats, different_show):
        assert (response.status_code, error_code(response)) == (409, "IDEMPOTENCY_KEY_REUSED")
    counts = (await client.get(f"/shows/{show['show_id']}")).json()["counts"]
    assert counts["confirmed"] == 1


async def test_the_per_user_limit_counts_what_is_already_held(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1", "A2", "A3"], per_user_limit=2)
    _, headers = await new_guest()
    await reserve(client, show["show_id"], headers, ["A1", "A2"])

    over = await reserve(client, show["show_id"], headers, ["A3"])
    too_many_at_once = await reserve(client, show["show_id"], headers, ["A1", "A2", "A3"])

    assert (over.status_code, error_code(over)) == (409, "PER_USER_LIMIT")
    assert over.json()["error"]["details"] == {"limit": 2, "currently_held": 2}
    assert too_many_at_once.status_code == 422


async def test_request_level_rejections(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1"])
    url = f"/shows/{show['show_id']}/reserve"
    _, headers = await new_guest()

    no_key = await client.post(url, headers=headers, json={"seats": ["A1"]})
    key_conflict = await client.post(
        url,
        headers=headers | {"Idempotency-Key": "a"},
        json={"seats": ["A1"], "idempotency_key": "b"},
    )
    duplicates = await reserve(client, show["show_id"], headers, ["A1", "A1"])
    unknown_seat = await reserve(client, show["show_id"], headers, ["Z9"])
    unknown_show = await reserve(client, str(uuid4()), headers, ["A1"])
    anonymous = await client.post(url, headers={"Idempotency-Key": "x"}, json={"seats": ["A1"]})

    assert no_key.status_code == key_conflict.status_code == duplicates.status_code == 422
    assert (unknown_seat.status_code, error_code(unknown_seat)) == (404, "SEAT_NOT_FOUND")
    assert (unknown_show.status_code, error_code(unknown_show)) == (404, "SHOW_NOT_FOUND")
    assert (anonymous.status_code, error_code(anonymous)) == (401, "UNAUTHENTICATED")
    counts = (await client.get(f"/shows/{show['show_id']}")).json()["counts"]
    assert counts["available"] == 1
