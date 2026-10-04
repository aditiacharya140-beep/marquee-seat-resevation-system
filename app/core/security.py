"""Password hashing and access tokens (mds/05-auth-and-rbac.md)."""

import asyncio
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import settings
from app.core.constants import (
    JWT_ALGORITHM,
    SERVICE_NAME,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    Role,
)
from app.core.error_codes import ErrorCode
from app.core.errors import AuthError
from app.domain.models import Principal, User

_hasher = PasswordHasher()
# Bounded, so a login flood queues here instead of stalling the event loop that the
# reserve path runs on.
_executor = ThreadPoolExecutor(
    max_workers=settings.password_hash_workers, thread_name_prefix="argon2"
)
#: Verified on the miss path, so an unknown email costs what a wrong password costs.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe())


def _verify(password: str, password_hash: str | None) -> bool:
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return password_hash is not None


async def hash_password(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_executor, _hasher.hash, password)


async def verify_password(password: str, password_hash: str | None) -> bool:
    return await asyncio.get_running_loop().run_in_executor(
        _executor, _verify, password, password_hash
    )


def _issue(user: User, token_type: str, ttl: int) -> str:
    now = int(time.time())
    claims = {
        "sub": str(user.id),
        "role": user.role.value,
        "gst": user.is_guest,
        "typ": token_type,
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + ttl,
        "iss": SERVICE_NAME,
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


def issue_access_token(user: User) -> tuple[str, int]:
    ttl = settings.guest_token_ttl_seconds if user.is_guest else settings.access_token_ttl_seconds
    return _issue(user, TOKEN_TYPE_ACCESS, ttl), ttl


def issue_refresh_token(user: User) -> str:
    return _issue(user, TOKEN_TYPE_REFRESH, settings.refresh_token_ttl_seconds)


def _verify_token(token: str, token_type: str) -> Principal:
    """Every failure is the same 401: which check failed is not the caller's business."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            # Pinned to one algorithm: `none` and asymmetric variants are rejected.
            algorithms=[JWT_ALGORITHM],
            issuer=SERVICE_NAME,
            options={"require": ["sub", "exp", "iss", "typ", "role"]},
        )
        # Without this, the long-lived refresh token would work as an access token.
        if claims["typ"] != token_type:
            raise AuthError(ErrorCode.UNAUTHENTICATED)
        return Principal(
            user_id=UUID(claims["sub"]),
            role=Role(claims["role"]),
            is_guest=bool(claims.get("gst", False)),
        )
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise AuthError(ErrorCode.UNAUTHENTICATED) from None


def verify_access_token(token: str) -> Principal:
    return _verify_token(token, TOKEN_TYPE_ACCESS)


def verify_refresh_token(token: str) -> Principal:
    return _verify_token(token, TOKEN_TYPE_REFRESH)
