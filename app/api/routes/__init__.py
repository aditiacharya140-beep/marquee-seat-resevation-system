"""Router assembly."""

from fastapi import APIRouter

from app.api.routes import auth, health, metrics, pages, reservations, shows

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(shows.router)
api_router.include_router(reservations.router)
api_router.include_router(metrics.router)
api_router.include_router(pages.router)
