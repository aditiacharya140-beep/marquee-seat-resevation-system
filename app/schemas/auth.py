"""Auth bodies. No request model here or anywhere declares an identity field (ADR-028)."""

from uuid import UUID

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.constants import EMAIL_PATTERN, PASSWORD_MAX_LENGTH, TOKEN_TYPE_BEARER, Role
from app.domain.models import AuthSession, User


class Credentials(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=settings.password_min_length, max_length=PASSWORD_MAX_LENGTH)


class UserResponse(BaseModel):
    user_id: UUID
    email: str | None
    role: Role
    is_guest: bool

    @classmethod
    def of(cls, user: User) -> "UserResponse":
        return cls(user_id=user.id, email=user.email, role=user.role, is_guest=user.is_guest)


class TokenResponse(BaseModel):
    user_id: UUID
    email: str | None = None
    role: Role
    is_guest: bool
    access_token: str
    token_type: str = TOKEN_TYPE_BEARER
    expires_in: int

    @classmethod
    def of(cls, session: AuthSession) -> "TokenResponse":
        return cls(
            user_id=session.user.id,
            email=session.user.email,
            role=session.user.role,
            is_guest=session.user.is_guest,
            access_token=session.access_token,
            expires_in=session.expires_in,
        )
