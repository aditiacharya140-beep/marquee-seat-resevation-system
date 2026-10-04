"""The admin console's API. Every route requires the admin role."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import AdminUser
from app.core.config import settings
from app.core.constants import LogLevel
from app.services import admin_service, audit_service

router = APIRouter(prefix="/admin", tags=["admin"])

Window = Annotated[int, Query(ge=1, le=settings.admin_max_window_minutes)]
Limit = Annotated[int, Query(ge=1, le=settings.admin_max_rows)]


@router.get("/overview")
async def overview(
    _admin: AdminUser, window_minutes: Window = settings.admin_default_window_minutes
) -> dict[str, Any]:
    return await admin_service.overview(window_minutes)


@router.get("/shows")
async def shows(_admin: AdminUser) -> dict[str, Any]:
    return {"items": await admin_service.shows()}


@router.get("/audit")
async def audit(
    _admin: AdminUser,
    window_minutes: Window = settings.admin_default_window_minutes,
    limit: Limit = settings.admin_default_rows,
    before_id: int | None = None,
    status_code: int | None = None,
    outcome_code: str | None = None,
    request_id: UUID | None = None,
    user_id: UUID | None = None,
    show_id: UUID | None = None,
) -> dict[str, Any]:
    items = await audit_service.recent(
        window_minutes=window_minutes,
        before_id=before_id,
        status_code=status_code,
        outcome_code=outcome_code,
        request_id=request_id,
        user_id=user_id,
        show_id=show_id,
        limit=limit,
    )
    return {"items": items, "next_before_id": items[-1]["id"] if len(items) == limit else None}


@router.get("/logs")
async def logs(
    _admin: AdminUser,
    limit: Limit = settings.admin_default_rows,
    level: LogLevel | None = None,
    event: str | None = None,
    request_id: UUID | None = None,
) -> dict[str, Any]:
    return {
        "items": admin_service.logs(
            limit=limit,
            level=level.value if level else None,
            event=event,
            request_id=str(request_id) if request_id else None,
        )
    }
