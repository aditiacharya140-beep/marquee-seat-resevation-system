"""Per-request context. Nothing passes the correlation id through a function signature.

Read by the logger, the error handlers, the access log and — from Stage 1 — the
repository layer, which stamps `request_id` on every row it writes.
"""

from contextvars import ContextVar, Token

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
outcome_code_var: ContextVar[str | None] = ContextVar("outcome_code", default=None)


def get_request_id() -> str | None:
    return request_id_var.get()


def set_request_id(request_id: str) -> Token[str | None]:
    return request_id_var.set(request_id)


def get_outcome_code() -> str | None:
    return outcome_code_var.get()


def set_outcome_code(code: str) -> None:
    outcome_code_var.set(code)
