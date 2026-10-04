from uuid import uuid4

import asyncpg

from app.core.constants import Role
from app.core.error_codes import ErrorCode
from app.core.errors import ConflictError
from app.domain.models import User
from app.repositories.base import current_request_id


def _user(row: asyncpg.Record) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        role=Role(row["role"]),
        is_guest=row["is_guest"],
        password_hash=row["password_hash"],
    )


async def create_user(conn: asyncpg.Connection, email: str, password_hash: str, role: Role) -> User:
    """Uniqueness is the index's decision, not a prior existence check, which would race."""
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO users (id, email, password_hash, role, is_guest, request_id)
            VALUES ($1, $2, $3, $4, false, $5)
            RETURNING id, email, role, is_guest, password_hash
            """,
            uuid4(),
            email,
            password_hash,
            role.value,
            current_request_id(),
        )
    except asyncpg.UniqueViolationError:
        raise ConflictError(ErrorCode.EMAIL_TAKEN) from None
    return _user(row)


async def upsert_admin(conn: asyncpg.Connection, email: str, password_hash: str) -> None:
    """Make the configured admin exist with the configured password, whatever was
    there before. One statement, so two instances booting together cannot both insert."""
    await conn.execute(
        """
        INSERT INTO users (id, email, password_hash, role, is_guest, request_id)
        VALUES ($1, $2, $3, $4, false, $5)
        ON CONFLICT (LOWER(email)) WHERE email IS NOT NULL
        DO UPDATE SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role,
                      is_guest = false, updated_at = now()
        """,
        uuid4(),
        email,
        password_hash,
        Role.ADMIN.value,
        current_request_id(),
    )


async def create_guest(conn: asyncpg.Connection) -> User:
    row = await conn.fetchrow(
        """
        INSERT INTO users (id, role, is_guest, request_id)
        VALUES ($1, $2, true, $3)
        RETURNING id, email, role, is_guest, password_hash
        """,
        uuid4(),
        Role.USER.value,
        current_request_id(),
    )
    return _user(row)


async def upgrade_guest(
    conn: asyncpg.Connection, user_id: object, email: str, password_hash: str
) -> User | None:
    """One guarded UPDATE sets all three columns, so a half-upgraded row is never
    written and two concurrent upgrades have one winner. The id is unchanged, so every
    reservation the guest made stays theirs. `None` means the row was not a guest."""
    try:
        row = await conn.fetchrow(
            """
            UPDATE users
               SET email = $2, password_hash = $3, is_guest = false,
                   updated_at = now(), request_id = $4
             WHERE id = $1 AND is_guest = true
            RETURNING id, email, role, is_guest, password_hash
            """,
            user_id,
            email,
            password_hash,
            current_request_id(),
        )
    except asyncpg.UniqueViolationError:
        raise ConflictError(ErrorCode.EMAIL_TAKEN) from None
    return _user(row) if row else None


async def get_by_email(conn: asyncpg.Connection, email: str) -> User | None:
    row = await conn.fetchrow(
        """
        SELECT id, email, role, is_guest, password_hash
          FROM users WHERE LOWER(email) = LOWER($1)
        """,
        email,
    )
    return _user(row) if row else None


async def get_by_id(conn: asyncpg.Connection, user_id: object) -> User | None:
    row = await conn.fetchrow(
        "SELECT id, email, role, is_guest, password_hash FROM users WHERE id = $1",
        user_id,
    )
    return _user(row) if row else None


async def admin_exists(conn: asyncpg.Connection) -> bool:
    return bool(
        await conn.fetchval("SELECT EXISTS (SELECT 1 FROM users WHERE role = $1)", Role.ADMIN.value)
    )
