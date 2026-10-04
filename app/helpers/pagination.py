"""Keyset pagination shared by the list endpoints."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.core.error_codes import ErrorCode
from app.core.errors import ValidationError
from app.domain.models import Page
from app.utils.cursor import decode_cursor, encode_cursor

SortKey = tuple[datetime, UUID]


def parse_cursor(cursor: str | None) -> SortKey | None:
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except ValueError:
        raise ValidationError(ErrorCode.VALIDATION_ERROR, message="Invalid cursor") from None


def page_of[T](rows: list[T], limit: int, sort_key: Callable[[T], SortKey]) -> Page[T]:
    """`rows` was fetched with `limit + 1`: the extra row only says another page exists."""
    items = rows[:limit]
    more = len(rows) > limit
    return Page(items=items, next_cursor=encode_cursor(*sort_key(items[-1])) if more else None)
