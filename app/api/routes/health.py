"""Liveness and readiness. Public by design: no principal dependency is declared."""

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.constants import HEALTH_STATUS_OK, READY_STATUS, SERVICE_NAME
from app.schemas.common import HealthResponse, ReadinessResponse
from app.services import health_service

router = APIRouter(tags=["ops"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """200 whenever the process is alive. Touches no dependency (REQ-040)."""
    return HealthResponse(
        status=HEALTH_STATUS_OK,
        service=SERVICE_NAME,
        version=settings.service_version,
    )


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    response_model_exclude_none=True,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readyz() -> JSONResponse:
    readiness = await health_service.check_readiness()
    return JSONResponse(
        readiness.model_dump(exclude_none=True),
        status_code=status.HTTP_200_OK
        if readiness.status == READY_STATUS
        else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
