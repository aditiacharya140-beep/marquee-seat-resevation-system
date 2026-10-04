"""Application factory: lifespan, middleware chain, exception handlers, routers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app.api.routes import api_router
from app.core.config import settings
from app.core.constants import (
    SCOPE_REQUEST_ID,
    UNMATCHED_ROUTE_LABEL,
    Header,
    LogEvent,
    LogLevel,
)
from app.core.context import set_outcome_code
from app.core.error_codes import REGISTRY, ErrorCode
from app.core.errors import AppError, InternalError, NotFoundError, ValidationError
from app.core.logging import configure_logging, get_logger, level_number
from app.core.metrics import unhandled_exceptions_total
from app.db.engine import database
from app.middleware.access_log import AccessLogMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.services import auth_service

logger = get_logger(__name__)


def _request_id(request: Request) -> str | None:
    """From the scope, not the ContextVar: the catch-all runs in Starlette's
    ServerErrorMiddleware, which wraps — and so outlives — the request context."""
    value = request.scope.get(SCOPE_REQUEST_ID)
    return value if isinstance(value, str) else None


def _route_label(request: Request) -> str:
    return getattr(request.scope.get("route"), "path", UNMATCHED_ROUTE_LABEL)


def _error_response(error: AppError, request_id: str | None) -> JSONResponse:
    return JSONResponse(
        error.envelope(request_id),
        status_code=error.http_status,
        headers=error.headers | ({Header.REQUEST_ID: request_id} if request_id else {}),
    )


async def handle_app_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, AppError)
    request_id = _request_id(request)
    set_outcome_code(exc.code.value)
    logger.log(
        level_number(exc.log_level),
        LogEvent.APP_ERROR,
        # An `error` line with no stack is unactionable at 2am; a decline needs none.
        exc_info=exc if exc.log_level is LogLevel.ERROR else None,
        extra={
            "request_id": request_id,
            "outcome_code": exc.code.value,
            "status": exc.http_status,
            "route": _route_label(request),
        },
    )
    return _error_response(exc, request_id)


async def handle_validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    request_id = _request_id(request)
    set_outcome_code(ErrorCode.VALIDATION_ERROR.value)
    # Only location, reason and type: the rejected input is never echoed, because a
    # password that failed a length rule would come straight back in the body.
    fields = [
        {"loc": [str(part) for part in error["loc"]], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    error = ValidationError(ErrorCode.VALIDATION_ERROR, details={"fields": fields})
    logger.info(
        LogEvent.VALIDATION_FAILED,
        extra={
            "request_id": request_id,
            "outcome_code": error.code.value,
            "route": _route_label(request),
            "field_count": len(fields),
        },
    )
    return _error_response(error, request_id)


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    """The router's own 404 and 405 answer in the service's envelope (ADR-023)."""
    assert isinstance(exc, StarletteHTTPException)
    error: AppError
    if exc.status_code == REGISTRY[ErrorCode.ROUTE_NOT_FOUND].http_status:
        error = NotFoundError(ErrorCode.ROUTE_NOT_FOUND)
    elif exc.status_code == REGISTRY[ErrorCode.METHOD_NOT_ALLOWED].http_status:
        # The router's Allow header says which methods the path does accept.
        error = AppError(ErrorCode.METHOD_NOT_ALLOWED, headers=dict(exc.headers or {}))
    else:
        # Nothing in this service raises HTTPException, so any other status is a bug.
        logger.error(
            LogEvent.UNHANDLED_HTTP_EXCEPTION,
            extra={"request_id": _request_id(request), "status": exc.status_code},
        )
        error = InternalError(ErrorCode.INTERNAL_ERROR)
    return await handle_app_error(request, error)


async def handle_unexpected_error(request: Request, exc: Exception) -> Response:
    request_id = _request_id(request)
    route = _route_label(request)
    unhandled_exceptions_total.labels(route=route).inc()
    logger.error(
        LogEvent.UNHANDLED_EXCEPTION,
        exc_info=exc,
        extra={"request_id": request_id, "route": route},
    )
    # str(exc) never reaches the client: a 500 is a bug report about this service.
    return _error_response(InternalError(ErrorCode.INTERNAL_ERROR), request_id)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logger.info(LogEvent.STARTUP, extra={"config": settings.redacted_summary()})
    await database.connect()
    try:
        await auth_service.bootstrap_admin()
        yield
    finally:
        await database.close()
        logger.info(LogEvent.SHUTDOWN)


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="Seat Reservation Service",
        version=settings.service_version,
        lifespan=lifespan,
    )

    # Registration order is the reverse of execution: the last added is outermost, so
    # request context is established before any other layer runs (mds/07-middleware.md).
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)

    app.include_router(api_router)
    return app


app = create_app()
