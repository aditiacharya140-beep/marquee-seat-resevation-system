"""Single-line JSON logging on stdout. The platform ships it.

`event` is a stable snake_case identifier rather than a sentence, because log lines
are queried, not read (mds/08-error-logging.md).
"""

import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any, Final

import orjson

from app.core.config import settings
from app.core.constants import REDACTED, REDACTED_LOG_KEYS, SERVICE_NAME, LogLevel
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


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if str(key).lower() in REDACTED_LOG_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = {
            key: value for key, value in record.__dict__.items() if key not in _RECORD_ATTRS
        }
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
            payload["message"] = text

        payload.update(_redact(extra))

        if record.exc_info:
            payload["stack"] = "".join(traceback.format_exception(*record.exc_info)).strip()

        return orjson.dumps(payload, default=str).decode()


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
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
