"""Establishes correlation for everything downstream (mds/07-middleware.md).

Pure ASGI rather than `BaseHTTPMiddleware`: the latter runs the application in a
separate task, so a ContextVar set inside the application — the outcome code the
access log reports — would not be visible to the layers wrapping it.
"""

from uuid import UUID, uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.constants import SCOPE_REQUEST_ID, Header
from app.core.context import outcome_code_var, request_id_var


def _resolve_request_id(inbound: str | None) -> str:
    """Adopt an inbound id only when it is a UUID, otherwise mint one.

    A malformed value is replaced rather than rejected: it would end up in a `uuid`
    column and in log fields, and a client's bad header is not worth failing a booking
    over. An adopted id lets a caller correlate across its own retries.
    """
    if inbound:
        try:
            return str(UUID(inbound))
        except ValueError:
            pass
    return str(uuid4())


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(Headers(scope=scope).get(Header.REQUEST_ID))
        scope[SCOPE_REQUEST_ID] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault(Header.REQUEST_ID, request_id)
            await send(message)

        request_id_token = request_id_var.set(request_id)
        outcome_token = outcome_code_var.set(None)
        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(request_id_token)
            outcome_code_var.reset(outcome_token)
