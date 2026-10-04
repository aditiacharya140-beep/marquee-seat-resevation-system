"""One audit record per request, handed to a buffer that never blocks (REQ-046).

Innermost, so it sees the final status. It reads nothing from the body: the seat
labels, show and principal are facts the service and the auth dependency noted on
the request as they went.
"""

import time
from datetime import UTC, datetime
from uuid import UUID

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.constants import (
    AUDIT_EXEMPT_PREFIXES,
    AUDIT_MAX_PATH_LENGTH,
    SCOPE_REQUEST_ID,
    UNMATCHED_ROUTE_LABEL,
    Header,
)
from app.core.context import get_outcome_code, request_facts
from app.domain.models import AuditRecord
from app.middleware.rate_limit import client_address
from app.services import audit_service

_MS_PER_SECOND = 1000


def _exempt(path: str) -> bool:
    return path == "/" or any(path.startswith(prefix) for prefix in AUDIT_EXEMPT_PREFIXES)


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


class AuditMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.audit_enabled or _exempt(scope["path"]):
            await self.app(scope, receive, send)
            return

        status = 500
        started = time.perf_counter()
        occurred_at = datetime.now(UTC)

        async def send_capturing_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capturing_status)
        finally:
            facts = request_facts()
            headers = Headers(scope=scope)
            audit_service.enqueue(
                AuditRecord(
                    request_id=_uuid(scope.get(SCOPE_REQUEST_ID)),
                    occurred_at=occurred_at,
                    method=scope["method"],
                    path=scope["path"][:AUDIT_MAX_PATH_LENGTH],
                    route=getattr(scope.get("route"), "path", UNMATCHED_ROUTE_LABEL),
                    status_code=status,
                    duration_ms=round((time.perf_counter() - started) * _MS_PER_SECOND),
                    user_id=_uuid(facts.get("user_id")),
                    is_guest=facts.get("is_guest"),
                    outcome_code=get_outcome_code(),
                    show_id=_uuid(
                        facts.get("show_id") or scope.get("path_params", {}).get("show_id")
                    ),
                    seat_labels=facts.get("seat_labels"),
                    idempotency_key=headers.get(Header.IDEMPOTENCY_KEY)
                    or facts.get("idempotency_key"),
                    client_ip=client_address(scope, headers),
                )
            )
