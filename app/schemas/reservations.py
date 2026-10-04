"""Reservation bodies. No request model declares an identity field, so there is
nothing for a spoofed `user_id` to bind to (ADR-028); unknown fields are ignored."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    StrictInt,
    field_validator,
    model_serializer,
)

from app.core.config import settings
from app.core.constants import ReservationStatus
from app.domain.models import Page, Reservation
from app.schemas.shows import SeatLabel, reject_duplicate_labels


class ReserveRequest(BaseModel):
    seats: list[SeatLabel] = Field(min_length=1, max_length=settings.max_seats_per_show)
    idempotency_key: str | None = None
    hold_ttl_seconds: StrictInt | None = Field(default=None, gt=0)

    _unique_seats = field_validator("seats")(reject_duplicate_labels)


class ReservationResponse(BaseModel):
    reservation_id: UUID
    show_id: UUID
    user_id: UUID
    seats: list[str]
    amount_paise: int
    currency: str
    status: ReservationStatus
    expires_at: datetime | None = None
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime

    @model_serializer(mode="wrap")
    def _omit_what_does_not_apply(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """`expires_at` exists only on a hold and `confirmed_at` only once confirmed;
        each is absent rather than null, wherever a reservation is rendered."""
        return {key: value for key, value in handler(self).items() if value is not None}

    @classmethod
    def of(cls, reservation: Reservation) -> "ReservationResponse":
        return cls(
            reservation_id=reservation.id,
            show_id=reservation.show_id,
            user_id=reservation.user_id,
            seats=reservation.labels,
            amount_paise=reservation.amount_paise,
            currency=reservation.currency,
            status=reservation.status,
            expires_at=reservation.expires_at,
            confirmed_at=reservation.confirmed_at,
            cancelled_at=reservation.cancelled_at,
            created_at=reservation.created_at,
        )


class ReservationListResponse(BaseModel):
    items: list[ReservationResponse]
    next_cursor: str | None

    @classmethod
    def of(cls, page: Page[Reservation]) -> "ReservationListResponse":
        return cls(
            items=[ReservationResponse.of(reservation) for reservation in page.items],
            next_cursor=page.next_cursor,
        )
