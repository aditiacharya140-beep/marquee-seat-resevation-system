from fastapi import APIRouter, status

from app.api.deps import CurrentUser
from app.schemas.auth import (
    AccessTokenResponse,
    Credentials,
    RefreshRequest,
    TokenResponse,
    UserResponse,
)
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=TokenResponse,
    response_model_exclude_none=True,
)
async def register(body: Credentials) -> TokenResponse:
    return TokenResponse.of(await auth_service.register(body.email, body.password))


@router.post("/login", response_model=TokenResponse, response_model_exclude_none=True)
async def login(body: Credentials) -> TokenResponse:
    return TokenResponse.of(await auth_service.login(body.email, body.password))


@router.post(
    "/guest",
    status_code=status.HTTP_201_CREATED,
    response_model=TokenResponse,
    response_model_exclude_none=True,
)
async def guest() -> TokenResponse:
    return TokenResponse.of(await auth_service.guest())


@router.post("/upgrade", response_model=TokenResponse, response_model_exclude_none=True)
async def upgrade(body: Credentials, principal: CurrentUser) -> TokenResponse:
    return TokenResponse.of(await auth_service.upgrade(principal, body.email, body.password))


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(body: RefreshRequest) -> AccessTokenResponse:
    return AccessTokenResponse.of(await auth_service.refresh(body.refresh_token))


@router.get("/me", response_model=UserResponse)
async def me(principal: CurrentUser) -> UserResponse:
    return UserResponse.of(await auth_service.me(principal))
