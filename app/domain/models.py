"""Plain values passed between layers. No IO, no framework types."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.constants import (
    IdempotencyState,
    ReservationStatus,
    Role,
    SeatStatus,
    ShowStatus,
)


@dataclass(frozen=True, slots=True)
class User:
    id: UUID
    email: str | None
    role: Role
    is_guest: bool
    password_hash: str | None


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting. Built from a verified token and from nothing else."""

    user_id: UUID
    role: Role
    is_guest: bool


@dataclass(frozen=True, slots=True)
class AuthSession:
    user: User
    access_token: str
    expires_in: int
    #: Absent for a guest: a guest session is deliberately bounded; to persist, upgrade.
    refresh_token: str | None = None


@dataclass(frozen=True, slots=True)
class Show:
    id: UUID
    name: str
    event_kind: str
    price_paise: int
    currency: str
    per_user_limit: int
    hold_ttl_seconds: int
    total_seats: int
    status: ShowStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SeatView:
    """A seat as a reader sees it: effective status, never the stored column."""

    label: str
    status: SeatStatus
    price_paise: int
    section: str | None
    held_until: datetime | None


@dataclass(frozen=True, slots=True)
class ShowDetail:
    show: Show
    seats: list[SeatView]


@dataclass(frozen=True, slots=True)
class ClaimedSeat:
    id: UUID
    label: str
    price_paise: int


@dataclass(frozen=True, slots=True)
class Reservation:
    id: UUID
    show_id: UUID
    user_id: UUID
    status: ReservationStatus
    labels: list[str]
    amount_paise: int
    currency: str
    expires_at: datetime | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    id: UUID
    fingerprint: str
    state: IdempotencyState
    response_body: dict[str, Any] | None
    #: `in_progress` for longer than the staleness window: its owner is presumed dead.
    stale: bool


@dataclass(frozen=True, slots=True)
class ReserveOutcome:
    body: dict[str, Any]
    replayed: bool


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class AuditRecord:
    request_id: UUID | None
    occurred_at: datetime
    method: str
    path: str
    route: str
    status_code: int
    duration_ms: int
    user_id: UUID | None
    is_guest: bool | None
    outcome_code: str | None
    show_id: UUID | None
    seat_labels: list[str] | None
    idempotency_key: str | None
    client_ip: str | None
