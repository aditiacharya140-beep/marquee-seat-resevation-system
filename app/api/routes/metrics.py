from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST

from app.services import metrics_service

router = APIRouter(tags=["ops"])


@router.get("/metrics")
async def metrics() -> Response:
    return Response(await metrics_service.render(), media_type=CONTENT_TYPE_LATEST)
