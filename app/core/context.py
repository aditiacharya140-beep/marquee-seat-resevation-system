"""Per-request context. Nothing passes the correlation id through a function signature.

Read by the logger, the error handlers, the access log and — from Stage 1 — the
repository layer, which stamps `request_id` on every row it writes.
"""

from contextvars import ContextVar, Token
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
outcome_code_var: ContextVar[str | None] = ContextVar("outcome_code", default=None)
#: Facts about the request, noted as they become known — who, which show, which seats
#: — for the access log and the audit record. A dict that is mutated, not a variable
#: that is re-set, so a note made anywhere beneath the middleware is seen by it.
request_facts_var: ContextVar[dict[str, Any] | None] = ContextVar("request_facts", default=None)


def get_request_id() -> str | None:
    return request_id_var.get()


def set_request_id(request_id: str) -> Token[str | None]:
    return request_id_var.set(request_id)


def get_outcome_code() -> str | None:
    return outcome_code_var.get()


def set_outcome_code(code: str) -> None:
    outcome_code_var.set(code)


def request_facts() -> dict[str, Any]:
    return request_facts_var.get() or {}


def note(**facts: Any) -> None:
    current = request_facts_var.get()
    if current is not None:
        current.update(facts)
