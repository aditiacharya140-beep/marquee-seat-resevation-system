"""Shared fixtures for the Stage 0 suite (SEAT-006).

Two constraints shape this file.

**A real database, never the development one.** The client drives the real ASGI
application in-process through `httpx.ASGITransport`, against `TEST_DATABASE_URL`,
migrated by the same Alembic command the container entrypoint runs.

**Probe routes live here, not in `app/`.** Asserting the 422 and 500 correlation paths
needs a route that fails on purpose. Those routes are attached to a test-local
application built by the real `create_app()`, so the middleware chain and the exception
handlers under test are the production ones.

Every async test is wrapped in a hard timeout (`TEST_TIMEOUT_SECONDS`): a hung harness
must fail loudly rather than stall a run (LEARN-005).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]

#: `.env` is git-ignored, so CI and a fresh clone have no file for `Settings` to read.
#: These mirror `.env.example` and fill only the gaps: a value already in the
#: environment, or already declared in the `.env` that pydantic-settings will read,
#: wins. So a developer's local configuration is never silently overridden, and the
#: suite still runs on a clean checkout with nothing exported.
_FALLBACK_ENV: Final[dict[str, str]] = {
    "DATABASE_URL": "postgresql://seatres:seatres@localhost:5432/seatres",
    "TEST_DATABASE_URL": "postgresql://seatres:seatres@localhost:5432/seatres_test",
    "JWT_SECRET": "test-only-signing-key-padded-for-min-len",
    "ADMIN_EMAIL": "admin@example.com",
    "ADMIN_PASSWORD": "test-only-admin-password",
    "ALLOWED_EVENT_KINDS": "cinema,concert",
    "DEFAULT_EVENT_KIND": "cinema",
    "DEFAULT_CURRENCY": "INR",
}


def _env_file_values() -> dict[str, str]:
    """`Settings` resolves `env_file=".env"` against the working directory, so this
    reads the same file it will, rather than one next to this module."""
    env_file = Path(".env")
    if not env_file.exists():
        return {}
    return {
        line.split("=", 1)[0].strip().upper(): line.split("=", 1)[1].strip()
        for line in env_file.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


_DECLARED = _env_file_values()
for _name, _value in _FALLBACK_ENV.items():
    if _name not in os.environ and _name not in _DECLARED:
        os.environ[_name] = _value

# The suite owns its own database: the application under test must never write to the
# development one. The process environment outranks `.env`, so this redirects the pool
# and the migration subprocess alike.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or _DECLARED["TEST_DATABASE_URL"]
# A pool is opened per test (each test has its own event loop); one warm connection
# keeps that cheap, and the pool still grows to DB_POOL_MAX under a concurrency test.
os.environ["DB_POOL_MIN"] = "1"
# Every test client shares one address, so the limiter would throttle the suite
# itself. tests/integration/test_rate_limit.py switches it on for its own tests.
os.environ["RATE_LIMIT_ENABLED"] = "false"

import httpx  # noqa: E402  - must follow the environment bootstrap above
import pytest  # noqa: E402
from fastapi import APIRouter, FastAPI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.context import get_request_id  # noqa: E402
from app.core.error_codes import ErrorCode  # noqa: E402
from app.core.errors import ConflictError  # noqa: E402
from app.core.logging import JsonFormatter, configure_logging  # noqa: E402
from app.db.engine import database  # noqa: E402
from app.main import create_app, lifespan  # noqa: E402
from app.services import auth_service  # noqa: E402

TEST_TIMEOUT_SECONDS: Final = float(os.environ.get("TEST_TIMEOUT_SECONDS", "15"))

#: REQ-040: "completes in single-digit milliseconds". No `Settings` field expresses this
#: budget, so it is declared here and overridable by environment rather than inlined in
#: an assertion.
HEALTHZ_BUDGET_MS: Final = float(os.environ.get("HEALTHZ_BUDGET_MS", "10"))
HEALTHZ_SAMPLES: Final = int(os.environ.get("HEALTHZ_SAMPLES", "50"))

#: Distinctive enough that finding it in a response body proves a leak rather than a
#: coincidence.
PROBE_EXCEPTION_MESSAGE: Final = "probe-detonation-9f41c7-internal-detail"
PROBE_SECRET_VALUE: Final = "pw-7a21e0"

PROBE_OK_PATH: Final = "/__probe/ok"
PROBE_VALIDATE_PATH: Final = "/__probe/validate"
PROBE_BOOM_PATH: Final = "/__probe/boom"
PROBE_DECLINE_PATH: Final = "/__probe/decline"
PROBE_CONTEXT_PATH: Final = "/__probe/context"
PROBE_PARAM_TEMPLATE: Final = "/__probe/shows/{show_id}"

_UUID_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def is_uuid(value: str | None) -> bool:
    return value is not None and _UUID_RE.match(value) is not None


# --------------------------------------------------------------------------------------
# Hard timeout on every async test
# --------------------------------------------------------------------------------------


def _with_timeout(func: Callable[..., Any], seconds: float) -> Callable[..., Any]:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        async with asyncio.timeout(seconds):
            return await func(*args, **kwargs)

    return wrapper


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Bound every coroutine test, so a deadlock fails instead of hanging the run."""
    for item in items:
        func = getattr(item, "obj", None)
        if inspect.iscoroutinefunction(func):
            item.obj = _with_timeout(func, TEST_TIMEOUT_SECONDS)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Log capture
# --------------------------------------------------------------------------------------


class LogCapture:
    """Collects log lines as the configured `JsonFormatter` actually renders them.

    Asserting against the formatter's output rather than the raw `LogRecord` is the
    point: the redaction and the schema under test live in the formatter.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    @property
    def entries(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]

    def where(self, *, event: str | None = None, level: str | None = None) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self.entries
            if (event is None or entry.get("event") == event)
            and (level is None or entry.get("level") == level)
        ]

    def one(self, *, event: str, level: str | None = None) -> dict[str, Any]:
        matches = self.where(event=event, level=level)
        assert len(matches) == 1, (
            f"expected exactly one {event!r} line at level={level}, got {len(matches)}: "
            f"{[e.get('event') for e in self.entries]}"
        )
        return matches[0]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def clear(self) -> None:
        self.lines.clear()


class _CaptureHandler(logging.Handler):
    def __init__(self, capture: LogCapture) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self._capture = capture

    def emit(self, record: logging.LogRecord) -> None:
        # Only this service's own loggers. httpx logs a line per request through the
        # root logger, and it is not part of any contract under test.
        if record.name == "app" or record.name.startswith("app."):
            self._capture.lines.append(self.format(record))


@pytest.fixture(scope="session")
def configured_logging() -> None:
    """`configure_logging` replaces the root handler list, so it runs before capture."""
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@pytest.fixture
def log_capture(configured_logging: None) -> Iterator[LogCapture]:
    capture = LogCapture()
    handler = _CaptureHandler(capture)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


# --------------------------------------------------------------------------------------
# Applications and clients
# --------------------------------------------------------------------------------------


class ProbeBody(BaseModel):
    email: str
    #: A length rule is what makes the 422 fire while the submitted value stays
    #: password-shaped, which is the thing the response must not echo.
    password: str = Field(min_length=12)


def _probe_router() -> APIRouter:
    router = APIRouter()

    @router.get(PROBE_OK_PATH)
    async def probe_ok() -> dict[str, bool]:
        return {"ok": True}

    @router.post(PROBE_VALIDATE_PATH)
    async def probe_validate(body: ProbeBody) -> dict[str, str]:
        return {"email": body.email}

    @router.get(PROBE_BOOM_PATH)
    async def probe_boom() -> dict[str, bool]:
        raise RuntimeError(PROBE_EXCEPTION_MESSAGE)

    @router.get(PROBE_DECLINE_PATH)
    async def probe_decline() -> dict[str, bool]:
        raise ConflictError(ErrorCode.SEAT_TAKEN, details={"conflicts": ["A12"]})

    @router.get(PROBE_CONTEXT_PATH)
    async def probe_context() -> dict[str, str | None]:
        return {"request_id": get_request_id()}

    @router.get(PROBE_PARAM_TEMPLATE)
    async def probe_parameterised(show_id: str) -> dict[str, str]:
        return {"show_id": show_id}

    return router


@pytest.fixture(scope="session")
def probe_app(configured_logging: None) -> FastAPI:
    """The real application factory, plus routes that fail on purpose.

    `app/` gains nothing: the probes exist only for the lifetime of the test session.
    """
    application = create_app()
    application.include_router(_probe_router())
    return application


def _client(application: FastAPI, *, raise_app_exceptions: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        # LEARN-010: `ServerErrorMiddleware` re-raises after the 500 handler has
        # responded, so a client that re-raises application exceptions never sees the
        # envelope. Correlation on the 500 path is only assertable with this off.
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=raise_app_exceptions),
        base_url="http://seat-reservation.test",
        timeout=TEST_TIMEOUT_SECONDS,
    )


@pytest.fixture(scope="session")
def migrated() -> None:
    """The real migration, through the real entrypoint command, against the test database."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "app/alembic.ini", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )


@pytest.fixture
async def db(migrated: None) -> AsyncIterator[None]:
    """`ASGITransport` does not run the lifespan, so the pool is opened here instead."""
    await database.connect()
    try:
        yield
    finally:
        await database.close()


@pytest.fixture
async def client(probe_app: FastAPI, db: None) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(probe_app, raise_app_exceptions=False) as http_client:
        yield http_client


@pytest.fixture
async def strict_client(probe_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Re-raises whatever escapes the application, for asserting what escapes."""
    async with _client(probe_app, raise_app_exceptions=True) as http_client:
        yield http_client


@pytest.fixture
def app_lifespan() -> Callable[[FastAPI], Any]:
    return lifespan


@pytest.fixture
def resolved_settings() -> Any:
    return settings


# --------------------------------------------------------------------------------------
# Principals
# --------------------------------------------------------------------------------------


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin_headers(client: httpx.AsyncClient) -> dict[str, str]:
    await auth_service.bootstrap_admin()
    response = await client.post(
        "/auth/login",
        json={
            "email": settings.admin_email,
            "password": settings.admin_password.get_secret_value(),
        },
    )
    return bearer(response.json()["access_token"])


@pytest.fixture
def new_guest(client: httpx.AsyncClient) -> Callable[[], Any]:
    """Each call is a distinct principal: `(user_id, headers)`."""

    async def create() -> tuple[str, dict[str, str]]:
        body = (await client.post("/auth/guest")).json()
        return body["user_id"], bearer(body["access_token"])

    return create


@pytest.fixture
def new_show(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> Callable[..., Any]:
    async def create(seats: list[str], **fields: Any) -> dict[str, Any]:
        response = await client.post(
            "/shows",
            headers=admin_headers,
            json={"name": "test-show", "seats": seats, "price_paise": 25000} | fields,
        )
        assert response.status_code == 201, response.text
        return dict(response.json())

    return create


async def reserve(
    client: httpx.AsyncClient,
    show_id: str,
    headers: dict[str, str],
    seats: list[str],
    *,
    key: str | None = None,
    **body: Any,
) -> httpx.Response:
    return await client.post(
        f"/shows/{show_id}/reserve",
        headers=headers | {"Idempotency-Key": key or uuid4().hex},
        json={"seats": seats} | body,
    )


def error_code(response: httpx.Response) -> str | None:
    return response.json().get("error", {}).get("code")
