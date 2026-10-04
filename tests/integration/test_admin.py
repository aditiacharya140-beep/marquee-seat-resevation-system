"""The audit trail and the admin console's API (REQ-046)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.core.config import settings
from app.core.metrics import audit_records_dropped_total
from app.services import audit_service, auth_service
from tests.conftest import error_code, reserve


def _dropped() -> float:
    return sum(
        s.value
        for m in audit_records_dropped_total.collect()
        for s in m.samples
        if s.name.endswith("_total")
    )


async def _audit(client: httpx.AsyncClient, admin: dict[str, str], **params: Any) -> list[Any]:
    await audit_service.flush()
    response = await client.get("/admin/audit", headers=admin, params=params)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def test_a_reserve_is_audited_with_who_what_and_outcome(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    new_show: Callable[..., Any],
    new_guest: Callable[[], Any],
) -> None:
    show = await new_show(["A1"])
    user_id, guest = await new_guest()
    other_id, other = await new_guest()
    request_id = str(uuid4())

    won = await reserve(
        client, show["show_id"], guest | {"X-Request-ID": request_id}, ["A1"], key="audit-key"
    )
    lost = await reserve(client, show["show_id"], other, ["A1"])
    assert (won.status_code, lost.status_code) == (201, 409)

    [winner] = await _audit(client, admin_headers, request_id=request_id)
    assert winner["status_code"] == 201
    assert winner["route"] == "/shows/{show_id}/reserve"
    assert winner["user_id"] == user_id
    assert winner["is_guest"] is True
    assert winner["show_id"] == show["show_id"]
    assert winner["seat_labels"] == ["A1"]
    assert winner["idempotency_key"] == "audit-key"
    assert winner["outcome_code"] is None
    assert winner["duration_ms"] >= 0

    declined = await _audit(
        client, admin_headers, show_id=show["show_id"], outcome_code="SEAT_TAKEN"
    )
    assert [row["user_id"] for row in declined] == [other_id]
    assert declined[0]["status_code"] == 409


async def test_the_audit_trail_and_logs_are_admin_only(
    client: httpx.AsyncClient, new_guest: Callable[[], Any]
) -> None:
    _, guest = await new_guest()

    for path in ("/admin/overview", "/admin/audit", "/admin/logs", "/admin/shows"):
        as_guest = await client.get(path, headers=guest)
        anonymous = await client.get(path)
        assert (as_guest.status_code, error_code(as_guest)) == (403, "FORBIDDEN"), path
        assert (anonymous.status_code, error_code(anonymous)) == (401, "UNAUTHENTICATED"), path


async def test_a_full_buffer_drops_and_counts_but_never_fails_a_request(
    client: httpx.AsyncClient,
    new_show: Callable[..., Any],
    new_guest: Callable[[], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property the whole design exists for: audit is never backpressure."""
    seats = [f"S{i:02d}" for i in range(30)]
    show = await new_show(seats)
    principals = [headers for _, headers in [await new_guest() for _ in range(30)]]
    await audit_service.flush()
    monkeypatch.setattr(settings, "audit_queue_max", 1)
    before = _dropped()

    responses = await asyncio.gather(
        *(
            reserve(client, show["show_id"], headers, [seat])
            for headers, seat in zip(principals, seats, strict=True)
        )
    )

    assert [r.status_code for r in responses] == [201] * 30
    assert _dropped() - before == 29
    assert audit_service.queue_depth() == 1


async def test_the_overview_summarises_the_window_and_reports_the_system(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    new_show: Callable[..., Any],
    new_guest: Callable[[], Any],
) -> None:
    show = await new_show(["A1"])
    _, first = await new_guest()
    _, second = await new_guest()
    await reserve(client, show["show_id"], first, ["A1"])
    await reserve(client, show["show_id"], second, ["A1"])
    await audit_service.flush()

    response = await client.get("/admin/overview", headers=admin_headers)
    shows = await client.get("/admin/shows", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    requests = body["requests"]
    assert requests["total"] >= 2
    assert requests["by_status_class"]["2xx"] >= 1
    assert requests["by_status_class"]["4xx"] >= 1
    assert any(row["code"] == "SEAT_TAKEN" for row in requests["by_outcome"])
    reserve_route = next(r for r in requests["by_route"] if r["route"].endswith("/reserve"))
    assert reserve_route["p95_ms"] >= reserve_route["p50_ms"] >= 0
    assert requests["per_minute"]
    system = body["system"]
    assert system["pool"]["max"] == settings.db_pool_max
    assert system["audit"]["enabled"] is True
    assert "reservations_confirmed_total" in body["counters"]
    listed = next(item for item in shows.json()["items"] if item["show_id"] == show["show_id"])
    assert (listed["available"], listed["total_seats"]) == (0, 1)


async def test_recent_logs_are_readable_and_carry_no_secret(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    request_id = str(uuid4())
    await client.get("/shows", headers={"X-Request-ID": request_id})

    found: list[Any] = []
    for _ in range(50):  # the writer thread is asynchronous; give it a moment
        response = await client.get(
            "/admin/logs", headers=admin_headers, params={"request_id": request_id}
        )
        found = list(response.json()["items"])
        if found:
            break
        await asyncio.sleep(0.02)

    assert found, "the access-log line never reached the buffer"
    assert found[0]["event"] == "http_request"
    assert found[0]["request_id"] == request_id
    everything = (await client.get("/admin/logs", headers=admin_headers)).text
    assert settings.admin_password.get_secret_value() not in everything
    assert settings.jwt_secret.get_secret_value() not in everything


async def test_the_admin_account_follows_configuration(client: httpx.AsyncClient) -> None:
    """Booting twice leaves one working admin whose password is the configured one."""
    await auth_service.bootstrap_admin()
    await auth_service.bootstrap_admin()

    login = await client.post(
        "/auth/login",
        json={
            "email": settings.admin_email,
            "password": settings.admin_password.get_secret_value(),
        },
    )

    assert login.status_code == 200
    assert login.json()["role"] == "admin"


async def test_the_admin_page_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
