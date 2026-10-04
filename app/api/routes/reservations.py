from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser
from app.core.constants import Header as HeaderName
from app.schemas.reservations import ReservationResponse, ReserveRequest
from app.services import reservation_service

router = APIRouter(tags=["reservations"])


@router.post(
    "/shows/{show_id}/reserve",
    status_code=status.HTTP_201_CREATED,
    response_model=ReservationResponse,
    responses={status.HTTP_200_OK: {"model": ReservationResponse}},
)
async def reserve(
    show_id: UUID,
    body: ReserveRequest,
    principal: CurrentUser,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    outcome = await reservation_service.reserve(
        principal,
        show_id,
        body.seats,
        header_key=idempotency_key,
        body_key=body.idempotency_key,
        hold_ttl_seconds=body.hold_ttl_seconds,
    )
    if outcome.replayed:
        # 200, never 201: a retry must not be countable as a second creation (ADR-029).
        return JSONResponse(outcome.body, headers={HeaderName.IDEMPOTENT_REPLAY: "true"})
    return JSONResponse(outcome.body, status_code=status.HTTP_201_CREATED)


@router.post(
    "/reservations/{reservation_id}/confirm",
    response_model=ReservationResponse,
    response_model_exclude_none=True,
)
async def confirm(reservation_id: UUID, principal: CurrentUser) -> ReservationResponse:
    return ReservationResponse.of(await reservation_service.confirm(principal, reservation_id))


@router.post(
    "/reservations/{reservation_id}/cancel",
    response_model=ReservationResponse,
    response_model_exclude_none=True,
)
async def cancel(reservation_id: UUID, principal: CurrentUser) -> ReservationResponse:
    return ReservationResponse.of(await reservation_service.cancel(principal, reservation_id))


@router.get(
    "/reservations/{reservation_id}",
    response_model=ReservationResponse,
    response_model_exclude_none=True,
)
async def get_reservation(reservation_id: UUID, principal: CurrentUser) -> ReservationResponse:
    return ReservationResponse.of(await reservation_service.get(principal, reservation_id))
