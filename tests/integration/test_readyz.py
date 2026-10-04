"""`/readyz` and the schema it gates on (REQ-041)."""

from __future__ import annotations

import httpx

from app.core.constants import NOT_READY_STATUS, READY_STATUS, Header
from app.db.engine import database
from app.db.session import acquire
from tests.conftest import is_uuid

READYZ = "/readyz"


async def test_readyz_reports_ready_from_a_real_query(client: httpx.AsyncClient) -> None:
    response = await client.get(READYZ)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == READY_STATUS
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["database"]["latency_ms"] >= 0
    assert is_uuid(response.headers.get(Header.REQUEST_ID))


async def test_readyz_fails_closed_and_names_the_dependency(client: httpx.AsyncClient) -> None:
    await database.close()

    response = await client.get(READYZ)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == NOT_READY_STATUS
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["database"]["error"]


async def test_session_guards_are_applied_and_survive_release(db: None) -> None:
    """The pool issues RESET ALL on release; the guards must still be there after it."""
    for _ in range(2):
        async with acquire() as conn:
            assert await conn.fetchval("SHOW lock_timeout") == "2s"
            assert await conn.fetchval("SHOW statement_timeout") == "5s"
            assert await conn.fetchval("SHOW TimeZone") == "UTC"


async def test_the_migration_created_the_load_bearing_constraints(db: None) -> None:
    async with acquire() as conn:
        indexes = {r["indexname"] for r in await conn.fetch("SELECT indexname FROM pg_indexes")}
        checks = {r["conname"] for r in await conn.fetch("SELECT conname FROM pg_constraint")}

    assert {"uq_seat_active_claim", "uq_users_email", "ix_seats_by_holder"} <= indexes
    assert {"uq_seats_show_label", "ck_seats_hold_coherent", "uq_idem_user_key"} <= checks
