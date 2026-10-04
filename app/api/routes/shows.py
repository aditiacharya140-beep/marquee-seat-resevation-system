from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import AdminUser
from app.core.config import settings
from app.core.constants import ShowStatus
from app.schemas.shows import ShowCreate, ShowListResponse, ShowResponse
from app.services import show_service

router = APIRouter(prefix="/shows", tags=["shows"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ShowResponse,
    response_model_exclude_none=True,
)
async def create_show(body: ShowCreate, _admin: AdminUser) -> ShowResponse:
    detail = await show_service.create_show(
        name=body.name,
        labels=body.seats,
        price_paise=body.price_paise,
        event_kind=body.event_kind,
        currency=body.currency,
        per_user_limit=body.per_user_limit,
        hold_ttl_seconds=body.hold_ttl_seconds,
        seat_overrides={
            label: (override.price_paise, override.section)
            for label, override in body.seat_overrides.items()
        },
    )
    return ShowResponse.of(detail)


@router.get("", response_model=ShowListResponse)
async def list_shows(
    status: ShowStatus | None = None,
    event_kind: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=settings.page_size_max)] = settings.page_size_default,
) -> ShowListResponse:
    page = await show_service.list_shows(
        status=status, event_kind=event_kind, cursor=cursor, limit=limit
    )
    return ShowListResponse.of(page)


@router.get("/{show_id}", response_model=ShowResponse, response_model_exclude_none=True)
async def get_show(show_id: UUID) -> ShowResponse:
    return ShowResponse.of(await show_service.get_show(show_id))


@router.delete("/{show_id}")
async def delete_show(show_id: UUID, _admin: AdminUser) -> dict[str, object]:
    """Removes the show, its seats and every reservation on it. There is no undo."""
    deleted = await show_service.delete_show(show_id)
    return {"show_id": show_id, "deleted": deleted}
