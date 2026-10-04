"""The web page. Public by design: no principal dependency is declared."""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.core.constants import ADMIN_PAGE_PATH, INDEX_PATH, STATIC_DIR

router = APIRouter(include_in_schema=False)


@router.get(INDEX_PATH)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get(ADMIN_PAGE_PATH)
async def admin_console() -> FileResponse:
    """The page itself is public; everything it shows comes from admin-only routes."""
    return FileResponse(STATIC_DIR / "admin.html")
