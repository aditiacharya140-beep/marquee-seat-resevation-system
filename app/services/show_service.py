from uuid import UUID

from app.core.config import settings
from app.core.constants import ShowStatus
from app.core.error_codes import ErrorCode
from app.core.errors import NotFoundError, ValidationError
from app.db.session import acquire, transaction
from app.domain.models import Page, Show, ShowDetail
from app.helpers.pagination import page_of, parse_cursor
from app.repositories import show_repo


async def create_show(
    *,
    name: str,
    labels: list[str],
    price_paise: int,
    event_kind: str | None,
    currency: str | None,
    per_user_limit: int | None,
    hold_ttl_seconds: int | None,
    seat_overrides: dict[str, tuple[int | None, str | None]],
) -> ShowDetail:
    event_kind = event_kind or settings.default_event_kind
    if event_kind not in settings.allowed_event_kinds:
        raise ValidationError(
            ErrorCode.VALIDATION_ERROR,
            details={"event_kind": sorted(settings.allowed_event_kinds)},
        )
    labels = sorted(labels)
    overrides = [seat_overrides.get(label, (None, None)) for label in labels]
    async with transaction() as conn:
        show = await show_repo.create_show(
            conn,
            name=name,
            event_kind=event_kind,
            price_paise=price_paise,
            # Always supplied from here: a column default would be a second source of
            # truth for the same value (ADR-025).
            currency=currency or settings.default_currency,
            per_user_limit=per_user_limit or settings.default_per_user_limit,
            hold_ttl_seconds=hold_ttl_seconds or settings.default_hold_ttl_seconds,
            labels=labels,
            # NULL inherits the show's price at read and at claim time.
            seat_prices=[price for price, _ in overrides],
            seat_sections=[section for _, section in overrides],
        )
        seats = await show_repo.list_seats(conn, show)
    return ShowDetail(show=show, seats=seats)


async def get_show(show_id: UUID) -> ShowDetail:
    async with acquire() as conn:
        show = await show_repo.get_show(conn, show_id)
        if show is None:
            raise NotFoundError(ErrorCode.SHOW_NOT_FOUND)
        seats = await show_repo.list_seats(conn, show)
    return ShowDetail(show=show, seats=seats)


async def list_shows(
    *, status: ShowStatus | None, event_kind: str | None, cursor: str | None, limit: int
) -> Page[Show]:
    after = parse_cursor(cursor)
    async with acquire() as conn:
        shows = await show_repo.list_shows(
            conn,
            status=status.value if status else None,
            event_kind=event_kind,
            after=after,
            limit=limit + 1,
        )
    return page_of(shows, limit, lambda show: (show.created_at, show.id))
