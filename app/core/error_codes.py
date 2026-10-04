"""The single registry of error codes.

Every code the service can return appears here and nowhere else, so the client
contract in mds/06-apis.md is checkable against the code. A code's HTTP status,
default message and log level are properties of the code, not of the call site.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.constants import LogLevel


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    FORBIDDEN = "FORBIDDEN"
    SHOW_NOT_FOUND = "SHOW_NOT_FOUND"
    SEAT_NOT_FOUND = "SEAT_NOT_FOUND"
    RESERVATION_NOT_FOUND = "RESERVATION_NOT_FOUND"
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    SEAT_TAKEN = "SEAT_TAKEN"
    PER_USER_LIMIT = "PER_USER_LIMIT"
    EMAIL_TAKEN = "EMAIL_TAKEN"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    SHOW_NOT_ON_SALE = "SHOW_NOT_ON_SALE"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
    RESERVATION_CANCELLED = "RESERVATION_CANCELLED"
    RESERVATION_CONFIRMED = "RESERVATION_CONFIRMED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    NOT_READY = "NOT_READY"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class CodeSpec:
    http_status: int
    message: str
    log_level: LogLevel
    #: False for a domain decline the client can act on, True for a fault in this
    #: service. A decline is never logged at `error`: 20,000 losers of a seat race
    #: are not 20,000 errors (mds/08-error-logging.md).
    fault: bool


REGISTRY: Final[dict[ErrorCode, CodeSpec]] = {
    ErrorCode.UNAUTHENTICATED: CodeSpec(401, "Authentication required", LogLevel.INFO, False),
    ErrorCode.INVALID_CREDENTIALS: CodeSpec(401, "Invalid credentials", LogLevel.INFO, False),
    ErrorCode.FORBIDDEN: CodeSpec(403, "Not permitted", LogLevel.INFO, False),
    ErrorCode.SHOW_NOT_FOUND: CodeSpec(404, "Show not found", LogLevel.INFO, False),
    ErrorCode.SEAT_NOT_FOUND: CodeSpec(404, "Seat not found in this show", LogLevel.INFO, False),
    ErrorCode.RESERVATION_NOT_FOUND: CodeSpec(
        404, "Reservation not found", LogLevel.INFO, False
    ),
    ErrorCode.ROUTE_NOT_FOUND: CodeSpec(404, "No such route", LogLevel.INFO, False),
    ErrorCode.METHOD_NOT_ALLOWED: CodeSpec(405, "Method not allowed", LogLevel.INFO, False),
    ErrorCode.SEAT_TAKEN: CodeSpec(
        409, "One or more seats are no longer available", LogLevel.INFO, False
    ),
    ErrorCode.PER_USER_LIMIT: CodeSpec(
        409, "This would exceed your seat limit for this show", LogLevel.INFO, False
    ),
    ErrorCode.EMAIL_TAKEN: CodeSpec(409, "Email already registered", LogLevel.INFO, False),
    ErrorCode.ALREADY_REGISTERED: CodeSpec(
        409, "This account is already registered", LogLevel.INFO, False
    ),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: CodeSpec(
        409, "Idempotency key already used with a different request", LogLevel.INFO, False
    ),
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: CodeSpec(
        409, "An identical request is still in progress", LogLevel.INFO, False
    ),
    ErrorCode.SHOW_NOT_ON_SALE: CodeSpec(409, "This show is not on sale", LogLevel.INFO, False),
    ErrorCode.RESERVATION_EXPIRED: CodeSpec(409, "This hold has expired", LogLevel.INFO, False),
    ErrorCode.RESERVATION_CANCELLED: CodeSpec(
        409, "This reservation was cancelled", LogLevel.INFO, False
    ),
    ErrorCode.RESERVATION_CONFIRMED: CodeSpec(
        409, "This reservation is already confirmed", LogLevel.INFO, False
    ),
    ErrorCode.VALIDATION_ERROR: CodeSpec(422, "Request is not valid", LogLevel.INFO, False),
    ErrorCode.RATE_LIMITED: CodeSpec(429, "Too many requests", LogLevel.INFO, False),
    ErrorCode.DATABASE_UNAVAILABLE: CodeSpec(
        503, "Service dependency unavailable", LogLevel.ERROR, True
    ),
    ErrorCode.NOT_READY: CodeSpec(503, "Service not ready", LogLevel.ERROR, True),
    ErrorCode.INTERNAL_ERROR: CodeSpec(500, "Internal error", LogLevel.ERROR, True),
}
