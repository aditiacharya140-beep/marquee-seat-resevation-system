"""The JSON log formatter and its redaction filter (SEAT-003).

mds/08-error-logging.md names the primary rule — do not pass a secret to a log call —
and the filter as the defence against a future careless `extra=`. These tests are that
defence's regression cover: the keys are redacted, and the real values appear nowhere
in the rendered line, including inside nested structures.

The formatter is exercised through a real logger and a real handler rather than being
called directly, so what is asserted is what would actually reach stdout.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any, Final

import pytest

from app.core.constants import REDACTED, REDACTED_LOG_KEYS, SERVICE_NAME
from app.core.context import request_id_var
from app.core.logging import JsonFormatter, level_number

#: Distinctive, so finding one in the output is proof of a leak, not a coincidence.
SECRET_VALUES: Final[dict[str, str]] = {
    "password": "pw-c4f1a9-plaintext",
    "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$hash-b71e22",
    "token": "eyJhbGciOiJIUzI1NiJ9.tok-5de300.sig",
    "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.auth-9ab412.sig",
    "database_url": "postgresql://user:dsn-pw-77c105@db.internal:5432/seatres",
    "jwt_secret": "signing-key-3e90bb",
}

NESTED_SECRET: Final = "nested-pw-6b2d41"
DEEP_SECRET: Final = "deep-token-0f5c73"
LIST_SECRET: Final = "list-pw-ae1930"


class _Sink(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def sink() -> Iterator[_Sink]:
    handler = _Sink()
    logger = logging.getLogger("app.core.test_log_probe")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler
    finally:
        logger.handlers = []


@pytest.fixture
def log(sink: _Sink) -> logging.Logger:
    return logging.getLogger("app.core.test_log_probe")


def _one(sink: _Sink) -> dict[str, Any]:
    assert len(sink.lines) == 1, sink.lines
    return json.loads(sink.lines[0])


# ---------------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------------


def test_a_record_carrying_every_sensitive_key_emits_redacted_for_each(
    log: logging.Logger, sink: _Sink
) -> None:
    log.info("auth_probe", extra=dict(SECRET_VALUES))

    line = _one(sink)
    for key in SECRET_VALUES:
        assert line[key] == REDACTED, f"{key} was not redacted: {line[key]!r}"


def test_the_real_values_appear_nowhere_in_the_output(log: logging.Logger, sink: _Sink) -> None:
    log.info("auth_probe", extra=dict(SECRET_VALUES))

    rendered = sink.lines[0]
    for key, value in SECRET_VALUES.items():
        assert value not in rendered, f"{key} leaked into the log line"
    for fragment in ("dsn-pw-77c105", "tok-5de300", "hash-b71e22", "auth-9ab412"):
        assert fragment not in rendered, f"{fragment} leaked into the log line"


def test_a_nested_secret_is_redacted(log: logging.Logger, sink: _Sink) -> None:
    log.info(
        "auth_probe",
        extra={
            "principal": {
                "user_id": "u-1",
                "password": NESTED_SECRET,
                "session": {"token": DEEP_SECRET},
            }
        },
    )

    line = _one(sink)
    principal = line["principal"]
    assert principal["user_id"] == "u-1", "redaction ate a field it should have kept"
    assert principal["password"] == REDACTED
    assert principal["session"]["token"] == REDACTED
    assert NESTED_SECRET not in sink.lines[0]
    assert DEEP_SECRET not in sink.lines[0]


def test_a_secret_inside_a_list_of_dicts_is_redacted(log: logging.Logger, sink: _Sink) -> None:
    log.info("auth_probe", extra={"attempts": [{"email": "a@b.c", "password": LIST_SECRET}]})

    line = _one(sink)
    assert line["attempts"][0]["password"] == REDACTED
    assert line["attempts"][0]["email"] == "a@b.c"
    assert LIST_SECRET not in sink.lines[0]


@pytest.mark.parametrize("key", ["Authorization", "PASSWORD", "Token", "Password_Hash"])
def test_redaction_is_case_insensitive(log: logging.Logger, sink: _Sink, key: str) -> None:
    """A caller writing `Authorization` must not defeat the filter."""
    log.info("auth_probe", extra={"headers": {key: "value-should-not-survive"}})

    assert _one(sink)["headers"][key] == REDACTED
    assert "value-should-not-survive" not in sink.lines[0]


def test_the_redacted_key_set_is_the_documented_one() -> None:
    assert set(REDACTED_LOG_KEYS) == {
        "password",
        "password_hash",
        "token",
        "authorization",
        "database_url",
        "jwt_secret",
    }


def test_a_benign_field_is_not_redacted(log: logging.Logger, sink: _Sink) -> None:
    """Control: the filter targets named keys, it does not blanket-redact."""
    log.info("seat_claim_confirmed", extra={"show_id": "s-1", "seat_labels": ["A12"]})

    line = _one(sink)
    assert line["show_id"] == "s-1"
    assert line["seat_labels"] == ["A12"]


def test_redaction_also_applies_to_an_error_line_with_a_stack(
    log: logging.Logger, sink: _Sink
) -> None:
    try:
        raise RuntimeError("asyncpg: postgresql://u:SUPERSECRET-abc123@db.internal:5432/seatres")
    except RuntimeError as exc:
        log.error("unhandled_exception", exc_info=exc, extra={"token": SECRET_VALUES["token"]})

    line = _one(sink)
    assert line["token"] == REDACTED
    assert "Traceback (most recent call last)" in line["stack"]
    assert SECRET_VALUES["token"] not in sink.lines[0]
    # The property this test is named for: a secret inside the exception message must not
    # survive into `stack`. Driver errors routinely carry a DSN, and the catch-all is
    # exactly where they land.
    # Scoped to credentials in a URL, which is the realistic shape: a driver error carries
    # the DSN it failed to connect with. An arbitrary secret pasted into an exception
    # message is not pattern-matchable and is covered by the rule not to put one there.
    assert "SUPERSECRET-abc123" not in line["stack"], "a DSN password survived into the stack"
    assert "db.internal" in line["stack"], "scrubbing must keep the diagnostic useful"


# ---------------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------------


def test_every_line_carries_the_required_fields(log: logging.Logger, sink: _Sink) -> None:
    log.info("http_request")

    line = _one(sink)
    assert {"ts", "level", "event", "request_id", "service", "version"} <= set(line)
    assert line["service"] == SERVICE_NAME
    assert line["event"] == "http_request"


def test_the_timestamp_is_utc_with_microsecond_precision(log: logging.Logger, sink: _Sink) -> None:
    log.info("http_request")

    ts = _one(sink)["ts"]
    assert ts.endswith("Z")
    assert len(ts.split(".")[1]) == len("000000Z")


def test_a_line_is_one_line(log: logging.Logger, sink: _Sink) -> None:
    """Multi-line output breaks every line-oriented shipper downstream."""
    try:
        raise RuntimeError("multi\nline\nfailure")
    except RuntimeError as exc:
        log.error("unhandled_exception", exc_info=exc)

    assert "\n" not in sink.lines[0]
    assert "\\n" in sink.lines[0], "the stack should be escaped, not dropped"


def test_the_request_id_comes_from_the_context_var(log: logging.Logger, sink: _Sink) -> None:
    token = request_id_var.set("ctx-id-1")
    try:
        log.info("http_request")
    finally:
        request_id_var.reset(token)

    assert _one(sink)["request_id"] == "ctx-id-1"


def test_an_explicit_request_id_overrides_the_context_var(log: logging.Logger, sink: _Sink) -> None:
    """The 500 handler runs after the ContextVar is reset and passes the id explicitly."""
    token = request_id_var.set("ctx-id-1")
    try:
        log.info("unhandled_exception", extra={"request_id": "scope-id-2"})
    finally:
        request_id_var.reset(token)

    line = _one(sink)
    assert line["request_id"] == "scope-id-2"


def test_request_id_is_null_outside_a_request(log: logging.Logger, sink: _Sink) -> None:
    assert request_id_var.get() is None
    log.info("startup")

    assert _one(sink)["request_id"] is None


def test_a_foreign_library_sentence_is_moved_out_of_the_event_field() -> None:
    """`event` stays queryable even when a dependency logs prose."""
    record = logging.LogRecord(
        name="httpx._client",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: GET http://x %s",
        args=('"200 OK"',),
        exc_info=None,
    )

    line = json.loads(JsonFormatter().format(record))

    assert line["event"] == "httpx__client"
    assert line["event"].isidentifier()
    assert line["message"] == 'HTTP Request: GET http://x "200 OK"'


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error"])
def test_the_level_field_is_lowercase_for_every_level(
    log: logging.Logger, sink: _Sink, level: str
) -> None:
    log.log(level_number(level), "probe_event")  # type: ignore[arg-type]

    assert _one(sink)["level"] == level
