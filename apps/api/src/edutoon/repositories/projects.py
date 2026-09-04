from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import projects


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: UUID
    owner_id: UUID
    title: str
    description: str | None
    source_type: str
    status: str
    language: str
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> ProjectRecord:
    return ProjectRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        title=row["title"],
        description=row["description"],
        source_type=row["source_type"],
        status=row["status"],
        language=row["language"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    owner_id: UUID,
    title: str,
    source_type: str,
    description: str | None = None,
    language: str = "en-GB",
) -> ProjectRecord:
    stmt = (
        projects.insert()
        .values(
            owner_id=owner_id,
            title=title,
            source_type=source_type,
            description=description,
            language=language,
        )
        .returning(*projects.c)
    )
    result = await session.execute(stmt)
    return _from_row(result.mappings().one())


async def get_by_id(session: AsyncSession, project_id: UUID) -> ProjectRecord | None:
    stmt = select(projects).where(projects.c.id == project_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def list_by_owner(
    session: AsyncSession, owner_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ProjectRecord]:
    stmt = select(projects).where(projects.c.owner_id == owner_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=projects, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
