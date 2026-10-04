"""Request correlation and the error envelope (SEAT-006; REQ-044, REQ-045).

The claim under test is narrow and total: one id per request, the same one in the
response header, in the error envelope and in every log line the request produced —
on a success, on a 422, and on a fault.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.constants import Header, LogEvent
from app.core.context import get_request_id
from app.core.error_codes import REGISTRY, ErrorCode
from tests.conftest import (
    PROBE_BOOM_PATH,
    PROBE_CONTEXT_PATH,
    PROBE_DECLINE_PATH,
    PROBE_EXCEPTION_MESSAGE,
    PROBE_OK_PATH,
    PROBE_SECRET_VALUE,
    PROBE_VALIDATE_PATH,
    LogCapture,
    is_uuid,
)

VALID_INBOUND_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
MALFORMED_INBOUND_ID = "not-a-uuid"

_INVALID_BODY: dict[str, str] = {"email": "someone@example.com", "password": PROBE_SECRET_VALUE}


def _header_id(response: httpx.Response) -> str:
    value = response.headers.get(Header.REQUEST_ID)
    assert value is not None, f"no {Header.REQUEST_ID} header on {response.status_code}"
    return value


def _envelope(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}, f"envelope has unexpected top-level keys: {sorted(body)}"
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}, sorted(error)
    return error


def _app_lines(capture: LogCapture) -> list[dict[str, Any]]:
    return capture.entries


# ---------------------------------------------------------------------------------
# One id, three paths
# ---------------------------------------------------------------------------------


async def test_success_carries_one_id_in_header_and_log(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    response = await client.get(PROBE_OK_PATH)

    assert response.status_code == 200
    header_id = _header_id(response)
    assert is_uuid(header_id)

    access_line = log_capture.one(event=LogEvent.HTTP_REQUEST.value)
    assert access_line["request_id"] == header_id
    assert access_line["status"] == 200


async def test_validation_error_carries_same_id_in_header_envelope_and_log(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    response = await client.post(PROBE_VALIDATE_PATH, json=_INVALID_BODY)

    assert response.status_code == 422
    header_id = _header_id(response)
    error = _envelope(response)

    assert error["code"] == ErrorCode.VALIDATION_ERROR.value
    assert error["request_id"] == header_id

    validation_line = log_capture.one(event=LogEvent.VALIDATION_FAILED.value, level="info")
    assert validation_line["request_id"] == header_id
    assert validation_line["outcome_code"] == ErrorCode.VALIDATION_ERROR.value

    access_line = log_capture.one(event=LogEvent.HTTP_REQUEST.value)
    assert access_line["request_id"] == header_id
    assert access_line["status"] == 422


async def test_unhandled_exception_carries_same_id_in_header_envelope_and_log(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    response = await client.get(PROBE_BOOM_PATH)

    assert response.status_code == 500
    header_id = _header_id(response)
    error = _envelope(response)

    assert error["code"] == ErrorCode.INTERNAL_ERROR.value
    assert error["request_id"] == header_id

    fault_line = log_capture.one(event=LogEvent.UNHANDLED_EXCEPTION.value, level="error")
    assert fault_line["request_id"] == header_id

    access_line = log_capture.one(event=LogEvent.HTTP_REQUEST.value)
    assert access_line["request_id"] == header_id
    assert access_line["status"] == 500


async def test_every_log_line_of_a_request_carries_a_request_id(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    """`request_id` is null only on pre-context startup lines (mds/08-error-logging.md)."""
    for path in (PROBE_OK_PATH, PROBE_BOOM_PATH, PROBE_DECLINE_PATH):
        log_capture.clear()
        await client.get(path)
        lines = _app_lines(log_capture)
        assert lines, f"no log lines for {path}"
        unidentified = [line for line in lines if not is_uuid(line.get("request_id"))]
        assert not unidentified, f"{path} produced lines with no request id: {unidentified}"


async def test_domain_decline_envelope_logs_at_info_without_a_stack(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    """A decline is `info` and carries no stack: 20,000 race losers are not 20,000 errors."""
    response = await client.get(PROBE_DECLINE_PATH)

    assert response.status_code == 409
    error = _envelope(response)
    assert error["code"] == ErrorCode.SEAT_TAKEN.value
    assert error["message"] == REGISTRY[ErrorCode.SEAT_TAKEN].message
    assert error["details"] == {"conflicts": ["A12"]}
    assert error["request_id"] == _header_id(response)

    app_error_line = log_capture.one(event=LogEvent.APP_ERROR.value)
    assert app_error_line["level"] == "info"
    assert app_error_line["outcome_code"] == ErrorCode.SEAT_TAKEN.value
    assert app_error_line["status"] == 409
    assert "stack" not in app_error_line

    access_line = log_capture.one(event=LogEvent.HTTP_REQUEST.value)
    assert access_line["outcome_code"] == ErrorCode.SEAT_TAKEN.value


# ---------------------------------------------------------------------------------
# Inbound id: adopted or replaced
# ---------------------------------------------------------------------------------


async def test_valid_inbound_request_id_is_adopted(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    response = await client.get(PROBE_OK_PATH, headers={Header.REQUEST_ID: VALID_INBOUND_ID})

    assert _header_id(response) == VALID_INBOUND_ID
    assert log_capture.one(event=LogEvent.HTTP_REQUEST.value)["request_id"] == VALID_INBOUND_ID


async def test_malformed_inbound_request_id_is_replaced_with_a_minted_uuid(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    response = await client.get(PROBE_OK_PATH, headers={Header.REQUEST_ID: MALFORMED_INBOUND_ID})

    header_id = _header_id(response)
    assert header_id != MALFORMED_INBOUND_ID
    assert is_uuid(header_id)
    assert MALFORMED_INBOUND_ID not in log_capture.text
    assert log_capture.one(event=LogEvent.HTTP_REQUEST.value)["request_id"] == header_id


async def test_inbound_id_is_adopted_on_the_error_paths_too(
    client: httpx.AsyncClient,
) -> None:
    """A caller correlating a failed retry needs its own id back, not a fresh one."""
    validation = await client.post(
        PROBE_VALIDATE_PATH, json=_INVALID_BODY, headers={Header.REQUEST_ID: VALID_INBOUND_ID}
    )
    assert _header_id(validation) == VALID_INBOUND_ID
    assert _envelope(validation)["request_id"] == VALID_INBOUND_ID

    fault = await client.get(PROBE_BOOM_PATH, headers={Header.REQUEST_ID: VALID_INBOUND_ID})
    assert _header_id(fault) == VALID_INBOUND_ID
    assert _envelope(fault)["request_id"] == VALID_INBOUND_ID


@pytest.mark.parametrize(
    "inbound",
    ["", "   ", "not-a-uuid", "12345", "3f2504e0-4f89-41d3-9a0c", "'; DROP TABLE seats; --"],
)
async def test_unusable_inbound_ids_are_all_replaced(
    client: httpx.AsyncClient, inbound: str
) -> None:
    response = await client.get(PROBE_OK_PATH, headers={Header.REQUEST_ID: inbound})

    header_id = _header_id(response)
    assert is_uuid(header_id)
    assert header_id != inbound


# ---------------------------------------------------------------------------------
# No ContextVar bleed
# ---------------------------------------------------------------------------------


async def test_sequential_requests_on_one_client_get_distinct_ids(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    ids = [_header_id(await client.get(PROBE_OK_PATH)) for _ in range(5)]

    assert len(set(ids)) == len(ids), f"ids repeated across sequential requests: {ids}"
    logged = [line["request_id"] for line in log_capture.where(event=LogEvent.HTTP_REQUEST.value)]
    assert logged == ids


async def test_an_adopted_id_does_not_leak_into_the_next_request(
    client: httpx.AsyncClient,
) -> None:
    adopted = await client.get(PROBE_OK_PATH, headers={Header.REQUEST_ID: VALID_INBOUND_ID})
    assert _header_id(adopted) == VALID_INBOUND_ID

    following = await client.get(PROBE_OK_PATH)
    assert _header_id(following) != VALID_INBOUND_ID


async def test_the_context_var_matches_the_header_and_is_reset_afterwards(
    client: httpx.AsyncClient,
) -> None:
    """The in-request view and the client's view are the same id, and nothing survives."""
    response = await client.get(PROBE_CONTEXT_PATH)

    assert response.json()["request_id"] == _header_id(response)
    assert get_request_id() is None, "the request id outlived the request"


async def test_concurrent_requests_do_not_share_an_id(
    client: httpx.AsyncClient,
) -> None:
    import asyncio

    responses = await asyncio.gather(*(client.get(PROBE_CONTEXT_PATH) for _ in range(10)))

    header_ids = [_header_id(response) for response in responses]
    in_request_ids = [response.json()["request_id"] for response in responses]
    assert header_ids == in_request_ids
    assert len(set(header_ids)) == 10, f"ids collided under overlap: {header_ids}"


# ---------------------------------------------------------------------------------
# The catch-all leaks nothing
# ---------------------------------------------------------------------------------


async def test_catch_all_returns_a_generic_internal_error(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(PROBE_BOOM_PATH)

    assert response.status_code == 500
    error = _envelope(response)
    assert error["code"] == ErrorCode.INTERNAL_ERROR.value
    assert error["message"] == REGISTRY[ErrorCode.INTERNAL_ERROR].message
    assert error["details"] is None


async def test_catch_all_leaks_no_internals_to_the_client(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(PROBE_BOOM_PATH)

    body = response.text
    assert PROBE_EXCEPTION_MESSAGE not in body
    for leak in ("RuntimeError", "Traceback", 'File "', "app/main.py", "site-packages"):
        assert leak not in body, f"500 body leaked {leak!r}: {body}"


async def test_an_unhandled_exception_still_escapes_to_server_error_middleware(
    strict_client: httpx.AsyncClient,
) -> None:
    """A tripwire on an open gap, not an endorsement of it.

    ADR-024 requires the catch-all to be a middleware boundary immediately inside the
    request context, so that nothing escapes to be re-raised and logged a second time
    outside the correlation context (LEARN-010). That boundary is not implemented:
    `main.py` registers a handler for `Exception` only, which Starlette installs on
    `ServerErrorMiddleware`, and that re-raises after responding. SEAT-066 owns the fix.

    Until then this records the actual behaviour. When SEAT-066 lands, this test fails
    and must be replaced by its inverse — which is the point: the gap then closes
    deliberately instead of silently.
    """
    with pytest.raises(RuntimeError, match=PROBE_EXCEPTION_MESSAGE):
        await strict_client.get(PROBE_BOOM_PATH)


async def test_unhandled_exception_log_line_carries_a_stack_trace(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    await client.get(PROBE_BOOM_PATH)

    fault_line = log_capture.one(event=LogEvent.UNHANDLED_EXCEPTION.value, level="error")
    stack = fault_line.get("stack")
    assert stack, "an `error` line with no stack is unactionable at 2am"
    assert "Traceback (most recent call last)" in stack
    assert PROBE_EXCEPTION_MESSAGE in stack
    assert "RuntimeError" in stack


async def test_the_fault_log_line_names_the_route_template(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    await client.get(PROBE_BOOM_PATH)

    assert log_capture.one(event=LogEvent.UNHANDLED_EXCEPTION.value)["route"] == PROBE_BOOM_PATH


# ---------------------------------------------------------------------------------
# A 422 never echoes the input
# ---------------------------------------------------------------------------------


async def test_validation_error_does_not_echo_submitted_input(
    client: httpx.AsyncClient,
) -> None:
    """A password that failed a length rule must not come straight back in the body."""
    response = await client.post(PROBE_VALIDATE_PATH, json=_INVALID_BODY)

    assert response.status_code == 422
    assert PROBE_SECRET_VALUE not in response.text, (
        f"the rejected value was echoed in the 422 body: {response.text}"
    )
    assert "input" not in response.text


async def test_validation_error_details_carry_only_location_reason_and_type(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(PROBE_VALIDATE_PATH, json=_INVALID_BODY)

    fields = _envelope(response)["details"]["fields"]
    assert fields, "a 422 with no field detail is a decline the client cannot act on"
    for field in fields:
        assert set(field) == {"loc", "msg", "type"}, sorted(field)
    assert ["body", "password"] in [field["loc"] for field in fields]


async def test_validation_error_does_not_echo_input_into_the_logs_either(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    await client.post(PROBE_VALIDATE_PATH, json=_INVALID_BODY)

    assert PROBE_SECRET_VALUE not in log_capture.text


async def test_a_malformed_json_body_is_a_422_in_the_envelope(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        PROBE_VALIDATE_PATH,
        content=b'{"email": "a@b.c", "password": ',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert _envelope(response)["code"] == ErrorCode.VALIDATION_ERROR.value


# ---------------------------------------------------------------------------------
# What the log lines are
# ---------------------------------------------------------------------------------


async def test_log_lines_are_single_line_json(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    await client.get(PROBE_BOOM_PATH)

    assert log_capture.lines
    for line in log_capture.lines:
        assert "\n" not in line, "a multi-line log record breaks line-oriented shipping"
        json.loads(line)


async def test_every_log_line_has_the_required_schema(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    await client.get(PROBE_DECLINE_PATH)

    for line in log_capture.entries:
        assert {"ts", "level", "event", "request_id", "service", "version"} <= set(line)
        assert line["ts"].endswith("Z")
        assert line["level"] in {"debug", "info", "warning", "error"}
        event = line["event"]
        assert event.isidentifier() and event.islower(), f"event is not queryable: {event!r}"
