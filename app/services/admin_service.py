"""What the admin console reads: the audit trail, recent logs and a system snapshot."""

import time
from datetime import UTC, datetime
from typing import Any

from prometheus_client import REGISTRY

from app.core.config import settings
from app.core.constants import ADMIN_PAGE_PATH, SERVICE_NAME, STATIC_URL_PREFIX
from app.core.logging import recent_log_lines
from app.db.engine import database
from app.db.session import acquire
from app.repositories import show_repo
from app.services import audit_service

_STARTED = time.monotonic()
_OWN_PATHS = (ADMIN_PAGE_PATH, f"{STATIC_URL_PREFIX}/")
_OWN_METRIC_PREFIXES = (
    "reservations_",
    "superseded_",
    "rate_limited",
    "unhandled_",
    "audit_",
)


def _counters() -> dict[str, float]:
    """The service's own counters, flattened: `name{label="value"}` -> value."""
    values: dict[str, float] = {}
    for family in REGISTRY.collect():
        # Counters only: a gauge here would show its value as of the last scrape.
        if family.type != "counter" or not family.name.startswith(_OWN_METRIC_PREFIXES):
            continue
        for sample in family.samples:
            if sample.name.endswith("_created"):
                continue
            labels = ",".join(f'{key}="{value}"' for key, value in sorted(sample.labels.items()))
            values[f"{sample.name}{{{labels}}}" if labels else sample.name] = sample.value
    return values


async def overview(window_minutes: int) -> dict[str, Any]:
    pool = database.pool
    return {
        "generated_at": datetime.now(UTC),
        "window_minutes": window_minutes,
        "requests": await audit_service.summary(window_minutes),
        "counters": _counters(),
        "system": {
            "service": SERVICE_NAME,
            "version": settings.service_version,
            "uptime_seconds": round(time.monotonic() - _STARTED),
            "rate_limit_enabled": settings.rate_limit_enabled,
            "pool": {
                "size": pool.get_size(),
                "idle": pool.get_idle_size(),
                "max": settings.db_pool_max,
            },
            "audit": {
                "enabled": settings.audit_enabled,
                "queue_depth": audit_service.queue_depth(),
                "queue_max": settings.audit_queue_max,
            },
        },
    }


async def shows() -> list[dict[str, Any]]:
    """Recent shows with what a claim would find available right now."""
    async with acquire() as conn:
        recent = await show_repo.list_shows(
            conn, status=None, event_kind=None, after=None, limit=settings.gauge_max_shows
        )
        available = await show_repo.available_by_show(conn, settings.gauge_max_shows)
    return [
        {
            "show_id": show.id,
            "name": show.name,
            "status": show.status.value,
            "price_paise": show.price_paise,
            "currency": show.currency,
            "total_seats": show.total_seats,
            "available": available.get(show.id, 0),
            "created_at": show.created_at,
        }
        for show in recent
    ]


def logs(
    *, limit: int, level: str | None, event: str | None, request_id: str | None
) -> list[dict[str, Any]]:
    """Newest first, from this process's in-memory buffer: lines since the last
    restart, already redacted by the formatter that wrote them."""
    matched: list[dict[str, Any]] = []
    for line in recent_log_lines():
        # The console's own polling and the pages' files would otherwise be most of
        # what it shows. Asking for a request id still finds them.
        if not request_id and str(line.get("path", "")).startswith(_OWN_PATHS):
            continue
        if level and line.get("level") != level:
            continue
        if event and line.get("event") != event:
            continue
        if request_id and line.get("request_id") != request_id:
            continue
        matched.append(line)
        if len(matched) >= limit:
            break
    return matched
