"""Registration, login, guest sessions and the admin bootstrap."""

from app.core import security
from app.core.config import settings
from app.core.constants import LogEvent, Role
from app.core.error_codes import ErrorCode
from app.core.errors import AuthError, ConflictError
from app.core.logging import get_logger
from app.db.session import acquire
from app.domain.models import AuthSession, Principal, User
from app.repositories import user_repo

logger = get_logger(__name__)


def _session(user: User) -> AuthSession:
    token, expires_in = security.issue_access_token(user)
    return AuthSession(
        user=user,
        access_token=token,
        expires_in=expires_in,
        refresh_token=None if user.is_guest else security.issue_refresh_token(user),
    )


async def register(email: str, password: str) -> AuthSession:
    # Hashed before a connection is taken: Argon2 is tens of milliseconds, and a pool
    # slot held across it is a slot the reserve path cannot have.
    password_hash = await security.hash_password(password)
    async with acquire() as conn:
        user = await user_repo.create_user(conn, email, password_hash, Role.USER)
    return _session(user)


async def login(email: str, password: str) -> AuthSession:
    async with acquire() as conn:
        user = await user_repo.get_by_email(conn, email)
    verified = await security.verify_password(password, user.password_hash if user else None)
    if user is None or not verified:
        raise AuthError(ErrorCode.INVALID_CREDENTIALS)
    return _session(user)


async def guest() -> AuthSession:
    async with acquire() as conn:
        user = await user_repo.create_guest(conn)
    return _session(user)


async def upgrade(principal: Principal, email: str, password: str) -> AuthSession:
    """Give a guest credentials, keeping its id and so everything it has reserved."""
    if not principal.is_guest:
        raise ConflictError(ErrorCode.ALREADY_REGISTERED)
    password_hash = await security.hash_password(password)
    async with acquire() as conn:
        user = await user_repo.upgrade_guest(conn, principal.user_id, email, password_hash)
    if user is None:
        # A guest token that outlived its own upgrade.
        raise ConflictError(ErrorCode.ALREADY_REGISTERED)
    return _session(user)


async def refresh(refresh_token: str) -> AuthSession:
    """Stateless: the token is its own proof. The user is re-read so a role change or
    an upgrade since the token was issued is reflected in the new access token."""
    principal = security.verify_refresh_token(refresh_token)
    user = await me(principal)
    token, expires_in = security.issue_access_token(user)
    return AuthSession(user=user, access_token=token, expires_in=expires_in)


async def me(principal: Principal) -> User:
    async with acquire() as conn:
        user = await user_repo.get_by_id(conn, principal.user_id)
    if user is None:
        raise AuthError(ErrorCode.UNAUTHENTICATED)
    return user


async def bootstrap_admin() -> None:
    """The admin account is whatever configuration says it is: created if absent, and
    its password reset to the configured one on every start (ADR-037). Changing the
    admin's credentials is therefore a configuration change and a restart."""
    password_hash = await security.hash_password(settings.admin_password.get_secret_value())
    async with acquire() as conn:
        await user_repo.upsert_admin(conn, settings.admin_email, password_hash)
    logger.warning(LogEvent.ADMIN_BOOTSTRAPPED)
