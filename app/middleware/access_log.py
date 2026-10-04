"""One structured line per request, emitted on the way out (mds/07-middleware.md)."""

import time
from http import HTTPStatus

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.constants import ACCESS_LOG_EXEMPT_PATHS, UNMATCHED_ROUTE_LABEL, LogEvent
from app.core.context import get_outcome_code, request_facts
from app.core.logging import get_logger

logger = get_logger(__name__)

_MS_PER_SECOND = 1000


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in ACCESS_LOG_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        status = HTTPStatus.INTERNAL_SERVER_ERROR.value
        started = time.perf_counter()

        async def send_capturing_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capturing_status)
        except Exception:
            # The catch-all handler renders the 500 outside this layer, so record it here.
            self._log(scope, HTTPStatus.INTERNAL_SERVER_ERROR.value, started)
            raise
        self._log(scope, status, started)

    def _log(self, scope: Scope, status: int, started: float) -> None:
        route = scope.get("route")
        logger.info(
            LogEvent.HTTP_REQUEST,
            extra={
                "method": scope["method"],
                "path": scope["path"],
                # The path template, not the concrete path: a burst against 20,000 show
                # ids must group into one series instead of exploding cardinality.
                "route": getattr(route, "path", UNMATCHED_ROUTE_LABEL),
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * _MS_PER_SECOND, 3),
                "outcome_code": get_outcome_code(),
                "user_id": request_facts().get("user_id"),
            },
        )
