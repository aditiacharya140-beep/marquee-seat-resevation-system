"""Mechanics every repository shares."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import asyncpg

from app.core.constants import DeclineReason, LogEvent
from app.core.context import get_request_id
from app.core.error_codes import ErrorCode
from app.core.errors import ConflictError
from app.core.logging import get_logger

logger = get_logger(__name__)


def current_request_id() -> UUID | None:
    """Stamped on every row written, without threading it through a signature."""
    request_id = get_request_id()
    return UUID(request_id) if request_id else None


@contextmanager
def contention_is_a_decline() -> Iterator[None]:
    """Losing a lock wait is an answer, not a fault: 409, never 5xx (ADR-016, ADR-027).

    Wraps every statement on the reserve, confirm and cancel paths that can wait on a
    row lock — not only the claim. An unwrapped one turns a slow neighbour into a 500.
    """
    try:
        yield
    except asyncpg.LockNotAvailableError:
        raise ConflictError(
            ErrorCode.SEAT_TAKEN, details={"reason": DeclineReason.LOCK_TIMEOUT.value}
        ) from None
    except asyncpg.DeadlockDetectedError as exc:
        # The lock order makes this unreachable; if it fires, the order was broken.
        logger.error(LogEvent.CLAIM_DEADLOCK, exc_info=exc)
        raise ConflictError(
            ErrorCode.SEAT_TAKEN, details={"reason": DeclineReason.DEADLOCK.value}
        ) from None
