from collections import Counter
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.config import settings
from app.core.constants import CURRENCY_PATTERN, PRINTABLE_PATTERN, SeatStatus, ShowStatus
from app.domain.models import Page, Show, ShowDetail

SeatLabel = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=settings.max_seat_label_length,
        pattern=PRINTABLE_PATTERN,
    ),
]


def reject_duplicate_labels(labels: list[str]) -> list[str]:
    duplicated = sorted(label for label, count in Counter(labels).items() if count > 1)
    if duplicated:
        raise ValueError(f"duplicate seat labels: {duplicated}")
    return labels


class SeatOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price_paise: StrictInt | None = Field(default=None, ge=0)
    section: str | None = Field(
        default=None, min_length=1, max_length=50, pattern=PRINTABLE_PATTERN
    )


class ShowCreate(BaseModel):
    # The one body that becomes durable configuration, so a mistyped field fails loudly
    # instead of silently producing a show sold at the wrong price (ADR-028).
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, pattern=PRINTABLE_PATTERN)
    seats: list[SeatLabel] = Field(min_length=1, max_length=settings.max_seats_per_show)
    price_paise: StrictInt = Field(ge=0)
    event_kind: str | None = Field(default=None, pattern=PRINTABLE_PATTERN)
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)
    per_user_limit: StrictInt | None = Field(default=None, gt=0)
    hold_ttl_seconds: StrictInt | None = Field(default=None, gt=0, le=settings.max_hold_ttl_seconds)

    seat_overrides: dict[SeatLabel, SeatOverride] = Field(default_factory=dict)

    _unique_seats = field_validator("seats")(reject_duplicate_labels)

    @model_validator(mode="after")
    def _overrides_name_real_seats(self) -> "ShowCreate":
        unknown = sorted(set(self.seat_overrides) - set(self.seats))
        if unknown:
            raise ValueError(f"seat_overrides names labels not in seats: {unknown}")
        return self


class SeatCountsResponse(BaseModel):
    available: int
    held: int
    confirmed: int
    total: int


class SeatResponse(BaseModel):
    label: str
    status: SeatStatus
    price_paise: int
    section: str | None = None
    held_until: datetime | None = None


class ShowResponse(BaseModel):
    #: The brief's name for it. `show_id` is the same value, kept because it is what a
    #: reservation calls it and what every other response here uses.
    id: UUID
    show_id: UUID
    name: str
    event_kind: str
    status: ShowStatus
    price_paise: int
    currency: str
    per_user_limit: int
    hold_ttl_seconds: int
    total_seats: int
    counts: SeatCountsResponse
    seats: list[SeatResponse]
    created_at: datetime

    @classmethod
    def of(cls, detail: ShowDetail) -> "ShowResponse":
        show, seats = detail.show, detail.seats
        tally = Counter(seat.status for seat in seats)
        return cls(
            id=show.id,
            show_id=show.id,
            name=show.name,
            event_kind=show.event_kind,
            status=show.status,
            price_paise=show.price_paise,
            currency=show.currency,
            per_user_limit=show.per_user_limit,
            hold_ttl_seconds=show.hold_ttl_seconds,
            total_seats=show.total_seats,
            counts=SeatCountsResponse(
                available=tally[SeatStatus.AVAILABLE],
                held=tally[SeatStatus.HELD],
                confirmed=tally[SeatStatus.CONFIRMED],
                total=len(seats),
            ),
            seats=[
                SeatResponse(
                    label=seat.label,
                    status=seat.status,
                    price_paise=seat.price_paise,
                    section=seat.section,
                    held_until=seat.held_until,
                )
                for seat in seats
            ],
            created_at=show.created_at,
        )


class ShowSummary(BaseModel):
    """No availability counts: a list of shows must not scan every seat of every show.
    Exact availability is the job of `GET /shows/{id}`."""

    show_id: UUID
    name: str
    event_kind: str
    status: ShowStatus
    price_paise: int
    currency: str
    total_seats: int
    created_at: datetime


class ShowListResponse(BaseModel):
    items: list[ShowSummary]
    next_cursor: str | None

    @classmethod
    def of(cls, page: Page[Show]) -> "ShowListResponse":
        return cls(
            items=[
                ShowSummary(
                    show_id=show.id,
                    name=show.name,
                    event_kind=show.event_kind,
                    status=show.status,
                    price_paise=show.price_paise,
                    currency=show.currency,
                    total_seats=show.total_seats,
                    created_at=show.created_at,
                )
                for show in page.items
            ],
            next_cursor=page.next_cursor,
        )
