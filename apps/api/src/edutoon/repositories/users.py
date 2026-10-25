from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import users


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: UUID
    clerk_user_id: str | None
    email: str
    display_name: str | None
    status: str
    role: str
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> UserRecord:
    return UserRecord(
        id=row["id"],
        clerk_user_id=row["clerk_user_id"],
        email=row["email"],
        display_name=row["display_name"],
        status=row["status"],
        role=row["role"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    email: str,
    role: str,
    clerk_user_id: str | None = None,
    display_name: str | None = None,
) -> UserRecord:
    """Insert a user. ``role`` is required on purpose: there is no default role, so nothing
    can create an account without choosing one (and the column has no default either)."""
    stmt = (
        users.insert()
        .values(email=email, role=role, clerk_user_id=clerk_user_id, display_name=display_name)
        .returning(*users.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return _from_row(result.mappings().one())


async def get_by_id(session: AsyncSession, user_id: UUID) -> UserRecord | None:
    stmt = select(users).where(users.c.id == user_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def get_by_email(session: AsyncSession, email: str) -> UserRecord | None:
    stmt = select(users).where(users.c.email == email)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def get_by_clerk_user_id(session: AsyncSession, clerk_user_id: str) -> UserRecord | None:
    stmt = select(users).where(users.c.clerk_user_id == clerk_user_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def active_admin_exists(session: AsyncSession) -> bool:
    stmt = select(users.c.id).where(users.c.role == "admin", users.c.status == "active").limit(1)
    return (await session.execute(stmt)).first() is not None


async def lock_and_count_active_admins(session: AsyncSession) -> int:
    """Lock every active administrator's row and return how many there are, so two concurrent
    demotions cannot each see "there is another administrator" and leave none."""
    stmt = (
        select(users.c.id)
        .where(users.c.role == "admin", users.c.status == "active")
        .order_by(users.c.id)
        .with_for_update()
    )
    return len((await session.execute(stmt)).all())


async def get_by_id_for_update(session: AsyncSession, user_id: UUID) -> UserRecord | None:
    stmt = select(users).where(users.c.id == user_id).with_for_update()
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def update_role_and_status(
    session: AsyncSession, user_id: UUID, *, role: str, status: str
) -> UserRecord:
    stmt = (
        users.update()
        .where(users.c.id == user_id)
        .values(role=role, status=status)
        .returning(*users.c)
    )
    return _from_row((await session.execute(stmt)).mappings().one())


async def count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(users))).scalar_one())


async def list_page(
    session: AsyncSession, *, limit: int = 50, cursor: str | None = None
) -> Page[UserRecord]:
    stmt = select(users)
    rows, next_cursor = await paginate_rows(session, stmt, table=users, limit=limit, cursor=cursor)
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
