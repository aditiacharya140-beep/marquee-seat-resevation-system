"""The web page. Public by design: no principal dependency is declared."""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.core.constants import INDEX_PATH, STATIC_DIR

router = APIRouter(include_in_schema=False)


@router.get(INDEX_PATH)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
