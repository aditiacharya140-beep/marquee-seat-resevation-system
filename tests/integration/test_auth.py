"""Register, login, guest, and the token checks behind `Depends` (REQ-001..REQ-006)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest

from app.core.config import settings
from app.core.constants import JWT_ALGORITHM, SERVICE_NAME
from app.services import auth_service

PASSWORD = "correct-horse-battery"


def unique_email() -> str:
    return f"user-{uuid4().hex}@example.com"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(**overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(uuid4()),
        "role": "user",
        "gst": False,
        "typ": "access",
        "iat": now,
        "exp": now + 60,
        "iss": SERVICE_NAME,
    } | overrides
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


async def test_register_then_the_token_identifies_that_user(client: httpx.AsyncClient) -> None:
    email = unique_email()

    created = await client.post("/auth/register", json={"email": email, "password": PASSWORD})

    assert created.status_code == 201
    body = created.json()
    assert body["email"] == email
    assert (body["role"], body["is_guest"], body["token_type"]) == ("user", False, "bearer")
    assert "password" not in created.text

    me = await client.get("/auth/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json() == {
        "user_id": body["user_id"],
        "email": email,
        "role": "user",
        "is_guest": False,
    }


async def test_a_duplicate_email_is_declined_case_insensitively(client: httpx.AsyncClient) -> None:
    email = unique_email()
    await client.post("/auth/register", json={"email": email, "password": PASSWORD})

    again = await client.post("/auth/register", json={"email": email.upper(), "password": PASSWORD})

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "EMAIL_TAKEN"


async def test_login_succeeds_and_both_failures_are_indistinguishable(
    client: httpx.AsyncClient,
) -> None:
    email = unique_email()
    registered = await client.post("/auth/register", json={"email": email, "password": PASSWORD})

    ok = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    wrong = await client.post("/auth/login", json={"email": email, "password": PASSWORD + "x"})
    unknown = await client.post("/auth/login", json={"email": unique_email(), "password": PASSWORD})

    assert ok.status_code == 200
    assert ok.json()["user_id"] == registered.json()["user_id"]
    assert wrong.status_code == unknown.status_code == 401
    for response in (wrong, unknown):
        error = response.json()["error"]
        assert (error["code"], error["message"]) == ("INVALID_CREDENTIALS", "Invalid credentials")


async def test_a_guest_is_a_real_principal(client: httpx.AsyncClient) -> None:
    created = await client.post("/auth/guest")

    assert created.status_code == 201
    body = created.json()
    assert body["is_guest"] is True
    assert "email" not in body
    assert body["expires_in"] == settings.guest_token_ttl_seconds

    me = await client.get("/auth/me", headers=bearer(body["access_token"]))
    assert me.json()["user_id"] == body["user_id"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Basic abc"},
        bearer(_token(exp=int(time.time()) - 5)),
        bearer(_token(typ="refresh")),
        bearer(_token(iss="someone-else")),
        bearer(jwt.encode({"sub": str(uuid4())}, "k" * 32, algorithm=JWT_ALGORITHM)),
    ],
    ids=["missing", "garbage", "wrong-scheme", "expired", "wrong-typ", "wrong-issuer", "bad-sig"],
)
async def test_every_token_failure_is_the_same_401(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_a_short_password_is_rejected_without_being_echoed(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/auth/register", json={"email": unique_email(), "password": "short-pw-1"}
    )

    assert response.status_code == 422
    assert "short-pw-1" not in response.text


async def test_admin_bootstrap_is_idempotent_and_the_admin_can_log_in(
    client: httpx.AsyncClient,
) -> None:
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


async def test_login_issues_a_refresh_token_that_only_refreshes(client: httpx.AsyncClient) -> None:
    email = unique_email()
    registered = (
        await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    ).json()
    refresh_token = registered["refresh_token"]

    refreshed = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
    as_bearer = await client.get("/auth/me", headers=bearer(refresh_token))
    access_as_refresh = await client.post(
        "/auth/refresh", json={"refresh_token": registered["access_token"]}
    )

    assert refreshed.status_code == 200
    assert set(refreshed.json()) == {"access_token", "token_type", "expires_in"}
    me = await client.get("/auth/me", headers=bearer(refreshed.json()["access_token"]))
    assert me.json()["user_id"] == registered["user_id"]
    # The long-lived token must not work as an access token, nor the reverse.
    assert as_bearer.status_code == access_as_refresh.status_code == 401
    assert "refresh_token" not in (await client.post("/auth/guest")).json()


async def test_a_guest_upgrade_keeps_the_same_user(client: httpx.AsyncClient) -> None:
    guest = (await client.post("/auth/guest")).json()
    email = unique_email()
    taken = unique_email()
    await client.post("/auth/register", json={"email": taken, "password": PASSWORD})

    email_taken = await client.post(
        "/auth/upgrade",
        headers=bearer(guest["access_token"]),
        json={"email": taken, "password": PASSWORD},
    )
    still_guest = await client.get("/auth/me", headers=bearer(guest["access_token"]))
    upgraded = await client.post(
        "/auth/upgrade",
        headers=bearer(guest["access_token"]),
        json={"email": email, "password": PASSWORD},
    )
    again = await client.post(
        "/auth/upgrade",
        headers=bearer(guest["access_token"]),
        json={"email": unique_email(), "password": PASSWORD},
    )
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})

    assert (email_taken.status_code, email_taken.json()["error"]["code"]) == (409, "EMAIL_TAKEN")
    assert still_guest.json()["is_guest"] is True
    assert upgraded.status_code == 200
    body = upgraded.json()
    assert (body["user_id"], body["is_guest"], body["email"]) == (guest["user_id"], False, email)
    assert body["refresh_token"]
    assert (again.status_code, again.json()["error"]["code"]) == (409, "ALREADY_REGISTERED")
    assert login.json()["user_id"] == guest["user_id"]


async def test_two_concurrent_upgrades_of_one_guest_have_one_winner(
    client: httpx.AsyncClient,
) -> None:
    guest = (await client.post("/auth/guest")).json()

    responses = await asyncio.gather(
        *(
            client.post(
                "/auth/upgrade",
                headers=bearer(guest["access_token"]),
                json={"email": unique_email(), "password": PASSWORD},
            )
            for _ in range(2)
        )
    )

    assert sorted(r.status_code for r in responses) == [200, 409]
