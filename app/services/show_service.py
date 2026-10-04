from uuid import UUID

from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.errors import NotFoundError, ValidationError
from app.db.session import acquire, transaction
from app.domain.models import ShowDetail
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
) -> ShowDetail:
    event_kind = event_kind or settings.default_event_kind
    if event_kind not in settings.allowed_event_kinds:
        raise ValidationError(
            ErrorCode.VALIDATION_ERROR,
            details={"event_kind": sorted(settings.allowed_event_kinds)},
        )
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
            labels=sorted(labels),
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
