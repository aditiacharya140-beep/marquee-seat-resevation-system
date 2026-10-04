from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import AdminUser
from app.schemas.shows import ShowCreate, ShowResponse
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
    )
    return ShowResponse.of(detail)


@router.get("/{show_id}", response_model=ShowResponse, response_model_exclude_none=True)
async def get_show(show_id: UUID) -> ShowResponse:
    return ShowResponse.of(await show_service.get_show(show_id))
