"""The web page is served by the service itself (mds/18-frontend.md)."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.core.constants import Header
from app.middleware.rate_limit import classify
from tests.conftest import error_code

ASSETS = ("/static/app.css", "/static/app.js")


async def test_the_root_serves_the_page_and_names_its_assets(client: httpx.AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers[Header.REQUEST_ID]
    for asset in ASSETS:
        assert asset in response.text


@pytest.mark.parametrize("asset", ASSETS)
async def test_the_assets_the_page_names_are_served(client: httpx.AsyncClient, asset: str) -> None:
    response = await client.get(asset)

    assert response.status_code == 200
    assert response.text


async def test_loading_the_page_spends_no_read_allowance(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_read", "1/600s")
    address = {"X-Forwarded-For": "10.250.250.250"}

    pages = [await client.get(path, headers=address) for path in ("/", *ASSETS) for _ in range(3)]
    reads = [await client.get("/shows", headers=address) for _ in range(2)]

    assert {response.status_code for response in pages} == {200}
    assert [response.status_code for response in reads] == [200, 429]
    assert classify("GET", "/") is None
    assert classify("GET", "/static/app.js") is None
    # A prefix match on the directory name alone would exempt an API path that merely
    # starts with the same letters.
    assert classify("GET", "/staticky") is not None


@pytest.mark.parametrize("path", ["/static/missing.js", "/static/", "/missing"])
async def test_an_unknown_path_still_answers_in_the_envelope(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path)

    assert response.status_code == 404
    assert error_code(response) == "ROUTE_NOT_FOUND"
    assert response.json()["error"]["request_id"] == response.headers[Header.REQUEST_ID]
