"""The exception hierarchy. Services raise these and nothing else.

`HTTPException` is never raised in application code: it bypasses the envelope and
the code registry, so a client would receive two different error shapes depending
on which layer failed (mds/08-error-logging.md).
"""

from typing import Any

from app.core.constants import LogLevel
from app.core.error_codes import REGISTRY, ErrorCode


class AppError(Exception):
    """Base for every failure the service reports to a client."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        log_level: LogLevel | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        spec = REGISTRY[code]
        self.code = code
        self.http_status = spec.http_status
        self.message = message if message is not None else spec.message
        self.details = details
        self.headers = headers or {}
        self.log_level = log_level if log_level is not None else spec.log_level
        super().__init__(self.message)

    def envelope(self, request_id: str | None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


class AuthError(AppError):
    """401 — the principal could not be established."""


class PermissionError(AppError):  # noqa: A001 - the 403 outcome named in mds/08-error-logging.md
    """403 — the principal is known and not permitted."""


class NotFoundError(AppError):
    """404 — the resource does not exist, or is not this principal's to see."""


class ConflictError(AppError):
    """409 — a domain decline: the request was understood and refused."""


class ValidationError(AppError):
    """422 — the request is malformed beyond what a schema expresses."""


class RateLimitError(AppError):
    """429 — a transport ceiling, never a domain rule."""


class DependencyError(AppError):
    """503 — a dependency is genuinely unavailable."""


class InternalError(AppError):
    """500 — a fault in this service. The client is told nothing specific."""
