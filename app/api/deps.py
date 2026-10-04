"""Identity providers. A route states who may call it by which of these it depends on."""

from typing import Annotated

from fastapi import Depends, Header

from app.core import errors, security
from app.core.constants import TOKEN_TYPE_BEARER, Role
from app.core.error_codes import ErrorCode
from app.domain.models import Principal


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != TOKEN_TYPE_BEARER or not token.strip():
        raise errors.AuthError(ErrorCode.UNAUTHENTICATED)
    return security.verify_access_token(token.strip())


CurrentUser = Annotated[Principal, Depends(get_current_user)]


async def require_admin(principal: CurrentUser) -> Principal:
    if principal.role is not Role.ADMIN:
        raise errors.PermissionError(ErrorCode.FORBIDDEN)
    return principal


AdminUser = Annotated[Principal, Depends(require_admin)]
