"""Readiness: a real query on a pooled connection, never a cached answer (REQ-041)."""

import asyncio
import time

from app.core.config import settings
from app.core.constants import NOT_READY_STATUS, READY_STATUS
from app.db.session import acquire
from app.repositories import health_repo
from app.schemas.common import DependencyCheck, ReadinessResponse


async def check_readiness() -> ReadinessResponse:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(settings.readyz_timeout_seconds):
            async with acquire(settings.readyz_timeout_seconds) as conn:
                await health_repo.ping(conn)
    except Exception as exc:  # noqa: BLE001 - fails closed on anything at all
        # The class name of the root cause, never its message: a driver error
        # routinely carries the DSN it failed to connect with.
        cause = exc.__cause__ or exc
        return ReadinessResponse(
            status=NOT_READY_STATUS,
            checks={"database": DependencyCheck(ok=False, error=type(cause).__name__)},
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return ReadinessResponse(
        status=READY_STATUS,
        checks={"database": DependencyCheck(ok=True, latency_ms=latency_ms)},
    )
