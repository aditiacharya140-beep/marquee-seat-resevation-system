"""Liveness. Public by design: no principal dependency is declared."""

from fastapi import APIRouter

from app.core.config import settings
from app.core.constants import HEALTH_STATUS_OK, SERVICE_NAME
from app.schemas.common import HealthResponse

router = APIRouter(tags=["ops"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """200 whenever the process is alive. Touches no dependency (REQ-040)."""
    return HealthResponse(
        status=HEALTH_STATUS_OK,
        service=SERVICE_NAME,
        version=settings.service_version,
    )
