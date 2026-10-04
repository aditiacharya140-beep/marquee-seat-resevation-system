"""Single-line JSON logging on stdout. The platform ships it.

`event` is a stable snake_case identifier rather than a sentence, because log lines
are queried, not read (mds/08-error-logging.md).
"""

import atexit
import contextlib
import dataclasses
import logging
import queue
import re
import sys
import traceback
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener
from typing import Any, Final

import orjson
from pydantic import BaseModel

from app.core.config import settings
from app.core.constants import REDACTED, REDACTED_LOG_KEY_ATOMS, SERVICE_NAME, LogLevel
from app.core.context import get_request_id

#: Attributes the stdlib puts on every record; anything else came from `extra=`.
_RECORD_ATTRS: Final = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "stacklevel",
        "taskName",
        "thread",
        "threadName",
    }
)


_DSN_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)(?P<user>[^:/@\s]+):[^@/\s]+@")


def _scrub_text(text: str) -> str:
    """Remove URL credentials from free text.

    `message` and `stack` are strings, so the key filter cannot reach them, and a driver
    error routinely carries the DSN it failed to connect with.
    """
    return _DSN_CREDENTIALS.sub(rf"\g<scheme>\g<user>:{REDACTED}@", text)


def _redact(value: Any) -> Any:
    # A model or dataclass passed as `extra=` would otherwise be rendered whole by the
    # serializer, password field and all, without its keys ever being inspected.
    if isinstance(value, BaseModel):
        value = value.model_dump()
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return {
            key: REDACTED
            if any(atom in str(key).lower() for atom in REDACTED_LOG_KEY_ATOMS)
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = {key: value for key, value in record.__dict__.items() if key not in _RECORD_ATTRS}
        request_id = extra.pop("request_id", None) or get_request_id()
        text = record.getMessage()

        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "event": text,
            "request_id": request_id,
            "service": SERVICE_NAME,
            "version": settings.service_version,
        }
        # A foreign library logs sentences; keep `event` queryable and move the text.
        if not (text.isidentifier() and text.islower()):
            payload["event"] = record.name.replace(".", "_")
            payload["message"] = _scrub_text(text)

        payload.update(_redact(extra))

        if record.exc_info:
            payload["stack"] = _scrub_text(
                "".join(traceback.format_exception(*record.exc_info)).strip()
            )

        return orjson.dumps(payload, default=str).decode()


class _DroppingQueueHandler(QueueHandler):
    """Formats on the calling thread, where the request context is, and hands the
    finished line to the listener. A full queue drops the line: losing a log record is
    an inconvenience, stalling a booking behind one is a defect."""

    def enqueue(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(queue.Full):
            self.queue.put_nowait(record)


_listener: QueueListener | None = None


def configure_logging() -> None:
    """stdout is written from a worker thread, never the event loop: a consumer that
    stops draining would otherwise block the loop on write(2) and wedge every
    in-flight request (RISK-010)."""
    global _listener  # noqa: PLW0603 - one listener per process, replaced on reconfigure
    if _listener is not None:
        _listener.stop()
    records: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=settings.log_queue_max)
    # The queue carries already-rendered lines, so the stream handler adds no format.
    _listener = QueueListener(records, logging.StreamHandler(sys.stdout))
    _listener.start()
    atexit.register(_listener.stop)

    handler = _DroppingQueueHandler(records)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # AccessLogMiddleware emits http_request; uvicorn's own access line would duplicate it.
    logging.getLogger("uvicorn.access").disabled = True

    # Starlette's ServerErrorMiddleware re-raises after a custom 500 handler has
    # responded, so uvicorn logs the trace a second time. Strip uvicorn's own handlers
    # so that line is JSON on the same stream rather than plain text.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.asgi"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def level_number(level: LogLevel) -> int:
    return logging.getLevelNamesMapping()[level.upper()]
