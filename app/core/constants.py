"""Named values that would otherwise be repeated literals.

Status values match the `CHECK` constraints in mds/03-data-model.md exactly; a
mismatch here is a constraint violation at runtime rather than a type error.
"""

from enum import StrEnum
from typing import Final

SERVICE_NAME: Final = "seat-reservation"


class SeatStatus(StrEnum):
    AVAILABLE = "available"
    HELD = "held"
    CONFIRMED = "confirmed"


class ReservationStatus(StrEnum):
    HELD = "held"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ShowStatus(StrEnum):
    DRAFT = "draft"
    ON_SALE = "on_sale"
    CLOSED = "closed"


class IdempotencyState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Header(StrEnum):
    REQUEST_ID = "X-Request-ID"
    AUTHORIZATION = "Authorization"
    IDEMPOTENCY_KEY = "Idempotency-Key"
    IDEMPOTENT_REPLAY = "Idempotent-Replay"
    RETRY_AFTER = "Retry-After"
    RATELIMIT_LIMIT = "X-RateLimit-Limit"
    RATELIMIT_REMAINING = "X-RateLimit-Remaining"
    RATELIMIT_RESET = "X-RateLimit-Reset"


class LogEvent(StrEnum):
    """Subset of the catalogue in mds/08-error-logging.md emitted so far."""

    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    HTTP_REQUEST = "http_request"
    APP_ERROR = "app_error"
    VALIDATION_FAILED = "validation_failed"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    RESERVATION_CREATED = "reservation_created"
    RESERVATION_CANCELLED = "reservation_cancelled"
    RESERVATION_CONFIRMED = "reservation_confirmed"
    CLAIM_BACKSTOP_VIOLATED = "claim_backstop_violated"
    CLAIM_DEADLOCK = "claim_deadlock"
    IDEMPOTENCY_RELEASE_FAILED = "idempotency_release_failed"
    METRICS_GAUGE_UNAVAILABLE = "metrics_gauge_unavailable"
    ADMIN_BOOTSTRAPPED = "admin_bootstrapped"
    ADMIN_BOOTSTRAP_SKIPPED = "admin_bootstrap_skipped"


JWT_ALGORITHM: Final = "HS256"
TOKEN_TYPE_ACCESS: Final = "access"  # noqa: S105 - a claim value, not a credential
TOKEN_TYPE_BEARER: Final = "bearer"  # noqa: S105
#: Shape only. Deliverability is not something a pattern can establish.
EMAIL_PATTERN: Final = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
PASSWORD_MAX_LENGTH: Final = 128
CURRENCY_PATTERN: Final = r"^[A-Z]{3}$"

HEALTH_STATUS_OK: Final = "ok"

IDEMPOTENCY_OPERATION_RESERVE: Final = "reserve"


class DeclineReason(StrEnum):
    """`reservations_declined_total{reason}` values that are not simply an error code."""

    LOCK_TIMEOUT = "lock_timeout"
    DEADLOCK = "deadlock"
    ACTIVE_CLAIM_BACKSTOP = "active_claim_backstop"
    IDEMPOTENT_REPLAY = "idempotent_replay"


READY_STATUS: Final = "ready"
NOT_READY_STATUS: Final = "not_ready"

#: Correlation id is carried on the ASGI scope as well as in the ContextVar,
#: because Starlette's ServerErrorMiddleware runs outside RequestContextMiddleware
#: and therefore after the ContextVar has been reset.
SCOPE_REQUEST_ID: Final = "request_id"

#: A platform health check every few seconds would otherwise dominate log volume
#: (mds/07-middleware.md).
ACCESS_LOG_EXEMPT_PATHS: Final = frozenset({"/healthz", "/readyz", "/metrics"})

UNMATCHED_ROUTE_LABEL: Final = "unmatched"

REDACTED: Final = "[redacted]"

#: Keys whose values never reach a log line, whatever a future `extra=` passes.
DSN_REDACTED: Final = "redacted"

REDACTED_LOG_KEYS: Final = frozenset(
    {"password", "password_hash", "token", "authorization", "database_url", "jwt_secret"}
)

# Matched as substrings, against atoms rather than compound names: suffixing on
# "jwt_secret" left "secret" and "client_secret" unguarded, and suffixing on "token" left
# "tokens" unguarded. Over-redacting a benign field is the cheaper mistake.
REDACTED_LOG_KEY_ATOMS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "credential",
    "api_key",
    "apikey",
    "dsn",
    "database_url",
)
