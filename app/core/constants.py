"""Named values that would otherwise be repeated literals.

Status values match the `CHECK` constraints in mds/03-data-model.md exactly; a
mismatch here is a constraint violation at runtime rather than a type error.
"""

from enum import StrEnum
from pathlib import Path
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


class RouteClass(StrEnum):
    """Rate-limit classes. Each has a `RATE_LIMIT_<CLASS>` ceiling in configuration."""

    RESERVE = "reserve"
    READ = "read"
    AUTH = "auth"
    GUEST = "guest"
    ADMIN = "admin"


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
    UNHANDLED_HTTP_EXCEPTION = "unhandled_http_exception"
    READINESS_CHECK_FAILED = "readiness_check_failed"
    SHOW_DELETED = "show_deleted"
    RESERVATION_CREATED = "reservation_created"
    RESERVATION_CANCELLED = "reservation_cancelled"
    RESERVATION_CONFIRMED = "reservation_confirmed"
    CLAIM_BACKSTOP_VIOLATED = "claim_backstop_violated"
    CLAIM_DEADLOCK = "claim_deadlock"
    IDEMPOTENCY_RELEASE_FAILED = "idempotency_release_failed"
    METRICS_GAUGE_UNAVAILABLE = "metrics_gauge_unavailable"
    AUDIT_FLUSH_FAILED = "audit_flush_failed"
    AUDIT_SHUTDOWN_FLUSH_INCOMPLETE = "audit_shutdown_flush_incomplete"
    ADMIN_BOOTSTRAPPED = "admin_bootstrapped"
    ADMIN_BOOTSTRAP_SKIPPED = "admin_bootstrap_skipped"


JWT_ALGORITHM: Final = "HS256"
TOKEN_TYPE_ACCESS: Final = "access"  # noqa: S105 - a claim value, not a credential
TOKEN_TYPE_REFRESH: Final = "refresh"  # noqa: S105
TOKEN_TYPE_BEARER: Final = "bearer"  # noqa: S105
#: Shape only. Deliverability is not something a pattern can establish.
EMAIL_PATTERN: Final = r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+$"
PASSWORD_MAX_LENGTH: Final = 128
CURRENCY_PATTERN: Final = r"^[A-Z]{3}$"
#: No control characters. PostgreSQL text cannot hold NUL at all, so one reaching a
#: statement is a driver error, and a driver error on a domain path is a 500.
PRINTABLE_PATTERN: Final = r"^[^\x00-\x1f\x7f]+$"

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

#: The web page and its assets (mds/18-frontend.md). Loading them must not spend the
#: visitor's read allowance, so the rate limiter passes both through.
INDEX_PATH: Final = "/"
STATIC_URL_PREFIX: Final = "/static"
ADMIN_PAGE_PATH: Final = "/admin"

#: Not audited: probes, the pages' own files, and the admin console's polling, which
#: would otherwise fill the trail with the act of reading it.
AUDIT_EXEMPT_PREFIXES: Final = (
    "/healthz",
    "/readyz",
    "/metrics",
    "/static/",
    "/admin",
    "/docs",
    "/openapi.json",
)
AUDIT_MAX_PATH_LENGTH: Final = 500
STATIC_DIR: Final = Path(__file__).resolve().parents[1] / "static"

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
