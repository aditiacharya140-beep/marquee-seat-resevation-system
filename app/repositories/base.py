"""Mechanics every repository shares."""

from uuid import UUID

from app.core.context import get_request_id


def current_request_id() -> UUID | None:
    """Stamped on every row written, without threading it through a signature."""
    request_id = get_request_id()
    return UUID(request_id) if request_id else None
