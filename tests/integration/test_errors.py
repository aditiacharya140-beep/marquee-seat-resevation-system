"""Failures raised by the router itself answer in the service's envelope (SEAT-066)."""

from __future__ import annotations

import httpx

from app.core.constants import Header


async def test_an_unmatched_route_uses_the_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "ROUTE_NOT_FOUND"
    assert error["request_id"] == response.headers[Header.REQUEST_ID]


async def test_a_wrong_method_uses_the_envelope_and_keeps_allow(client: httpx.AsyncClient) -> None:
    response = await client.get("/auth/login")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert response.headers["allow"] == "POST"
