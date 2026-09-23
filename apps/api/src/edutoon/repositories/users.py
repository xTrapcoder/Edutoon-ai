from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
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
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> UserRecord:
    return UserRecord(
        id=row["id"],
        clerk_user_id=row["clerk_user_id"],
        email=row["email"],
        display_name=row["display_name"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    email: str,
    clerk_user_id: str | None = None,
    display_name: str | None = None,
) -> UserRecord:
    stmt = (
        users.insert()
        .values(email=email, clerk_user_id=clerk_user_id, display_name=display_name)
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


async def list_page(
    session: AsyncSession, *, limit: int = 50, cursor: str | None = None
) -> Page[UserRecord]:
    stmt = select(users)
    rows, next_cursor = await paginate_rows(session, stmt, table=users, limit=limit, cursor=cursor)
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
