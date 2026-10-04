"""The reserve path's rarer branches: a stuck key, a held lock, a lapsed hold."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from app.core.config import settings
from app.db.session import acquire
from app.repositories import idempotency_repo
from app.services.reservation_service import _fingerprint
from tests.conftest import error_code, reserve


async def _plant_in_progress_key(user_id: str, show_id: str, seats: list[str], key: str) -> None:
    """A key whose owner never finished, as a crashed worker would leave it."""
    async with acquire() as conn:
        await idempotency_repo.try_claim(
            conn,
            user_id=UUID(user_id),
            key=key,
            scope=f"reserve:{show_id}",
            fingerprint=_fingerprint(UUID(show_id), seats, None),
            retention_hours=1,
        )


async def test_a_key_still_in_progress_declines_after_a_bounded_wait(
    client: httpx.AsyncClient,
    new_show: Callable[..., Any],
    new_guest: Callable[[], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    show = await new_show(["A1"])
    user_id, headers = await new_guest()
    await _plant_in_progress_key(user_id, show["show_id"], ["A1"], "stuck")
    monkeypatch.setattr(settings, "idempotency_wait_ms", 150)

    response = await reserve(client, show["show_id"], headers, ["A1"], key="stuck")

    assert (response.status_code, error_code(response)) == (409, "IDEMPOTENCY_IN_PROGRESS")
    assert response.headers["Retry-After"] == str(settings.idempotency_retry_after_seconds)
    counts = (await client.get(f"/shows/{show['show_id']}")).json()["counts"]
    assert counts["available"] == 1


async def test_a_stale_key_is_reclaimed_and_reserves_exactly_once(
    client: httpx.AsyncClient,
    new_show: Callable[..., Any],
    new_guest: Callable[[], Any],
) -> None:
    show = await new_show(["A1"])
    user_id, headers = await new_guest()
    await _plant_in_progress_key(user_id, show["show_id"], ["A1"], "orphaned")
    async with acquire() as conn:
        await conn.execute(
            "UPDATE idempotency_keys SET created_at = now() - INTERVAL '1 hour' WHERE key = $1"
            " AND user_id = $2",
            "orphaned",
            UUID(user_id),
        )

    responses = await asyncio.gather(
        *(reserve(client, show["show_id"], headers, ["A1"], key="orphaned") for _ in range(8))
    )

    assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
    assert len({r.json()["reservation_id"] for r in responses}) == 1


async def test_a_lock_timeout_is_a_409_never_a_500(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1"])
    _, headers = await new_guest()
    blocker = await asyncpg.connect(settings.database_url)
    try:
        held = blocker.transaction()
        await held.start()
        await blocker.execute(
            "SELECT 1 FROM seats WHERE show_id = $1 FOR UPDATE", UUID(show["show_id"])
        )

        blocked = await reserve(client, show["show_id"], headers, ["A1"], key="after-timeout")

        await held.rollback()
    finally:
        await blocker.close()
    retried = await reserve(client, show["show_id"], headers, ["A1"], key="after-timeout")

    assert (blocked.status_code, error_code(blocked)) == (409, "SEAT_TAKEN")
    assert blocked.json()["error"]["details"] == {"reason": "lock_timeout"}
    # The decline released the key, so the same key is a real attempt once the lock is gone.
    assert retried.status_code == 201


async def test_a_lapsed_hold_reads_expired_frees_the_limit_and_cannot_be_cancelled(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1", "A2"], per_user_limit=1)
    _, headers = await new_guest()
    held = (await reserve(client, show["show_id"], headers, ["A1"], hold_ttl_seconds=1)).json()
    blocked = await reserve(client, show["show_id"], headers, ["A2"])

    await asyncio.sleep(1.2)
    read = await client.get(f"/reservations/{held['reservation_id']}", headers=headers)
    cancel = await client.post(f"/reservations/{held['reservation_id']}/cancel", headers=headers)
    after = await reserve(client, show["show_id"], headers, ["A2"])

    assert (blocked.status_code, error_code(blocked)) == (409, "PER_USER_LIMIT")
    assert read.json()["status"] == "expired"
    assert (cancel.status_code, error_code(cancel)) == (409, "RESERVATION_EXPIRED")
    assert cancel.json()["error"]["details"] == {"status": "expired"}
    assert after.status_code == 201


async def test_a_show_that_is_not_on_sale_declines(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1"])
    _, headers = await new_guest()
    async with acquire() as conn:
        await conn.execute(
            "UPDATE shows SET status = 'closed' WHERE id = $1", UUID(show["show_id"])
        )

    response = await reserve(client, show["show_id"], headers, ["A1"])

    assert (response.status_code, error_code(response)) == (409, "SHOW_NOT_ON_SALE")


async def test_every_row_a_reserve_writes_carries_its_request_id(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1"])
    _, headers = await new_guest()
    request_id = str(uuid4())

    response = await reserve(
        client, show["show_id"], headers | {"X-Request-ID": request_id}, ["A1"]
    )

    assert response.headers["X-Request-ID"] == request_id
    async with acquire() as conn:
        seat = await conn.fetchrow(
            "SELECT request_id, version FROM seats WHERE show_id = $1", UUID(show["show_id"])
        )
        reservation = await conn.fetchval(
            "SELECT request_id FROM reservations WHERE id = $1",
            UUID(response.json()["reservation_id"]),
        )
    assert str(seat["request_id"]) == str(reservation) == request_id
    assert seat["version"] == 1


async def test_only_the_owner_may_confirm_or_read(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    show = await new_show(["A1"])
    _, owner = await new_guest()
    _, other = await new_guest()
    held = (await reserve(client, show["show_id"], owner, ["A1"], hold_ttl_seconds=60)).json()
    base = f"/reservations/{held['reservation_id']}"

    confirm = await client.post(f"{base}/confirm", headers=other)
    read = await client.get(base, headers=other)

    for response in (confirm, read):
        assert (response.status_code, error_code(response)) == (404, "RESERVATION_NOT_FOUND")
    assert (await client.get(base, headers=owner)).json()["status"] == "held"


async def test_an_owner_whose_key_was_taken_over_cannot_also_reserve(
    client: httpx.AsyncClient, new_show: Callable[..., Any], new_guest: Callable[[], Any]
) -> None:
    """The dangerous half of stale recovery: the "dead" owner turns out to be alive.
    Its key id no longer exists, so its claim must stop before it touches a seat."""
    show = await new_show(["A1"])
    user_id, _ = await new_guest()
    await _plant_in_progress_key(user_id, show["show_id"], ["A1"], "contested")
    async with acquire() as conn:
        record = await idempotency_repo.get(conn, UUID(user_id), "contested", 0)
        assert record is not None
        new_id = await idempotency_repo.reclaim_if_stale(conn, record.id, -1)
        assert new_id is not None and new_id != record.id

        async with conn.transaction():
            assert await idempotency_repo.lock_owned(conn, record.id) is False
            assert await idempotency_repo.lock_owned(conn, new_id) is True
        # The old owner's cleanup must not delete the new owner's key either.
        await idempotency_repo.release(conn, record.id)
        assert await idempotency_repo.get(conn, UUID(user_id), "contested", 0) is not None
