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
from edutoon.db.tables import project_topics


@dataclass(frozen=True, slots=True)
class ProjectTopicRecord:
    id: UUID
    project_id: UUID
    parent_id: UUID | None
    title: str
    summary: str | None
    position: int
    depth: int
    status: str
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> ProjectTopicRecord:
    return ProjectTopicRecord(
        id=row["id"],
        project_id=row["project_id"],
        parent_id=row["parent_id"],
        title=row["title"],
        summary=row["summary"],
        position=row["position"],
        depth=row["depth"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    project_id: UUID,
    title: str,
    parent_id: UUID | None = None,
    summary: str | None = None,
    position: int = 0,
    depth: int = 0,
) -> ProjectTopicRecord:
    stmt = (
        project_topics.insert()
        .values(
            project_id=project_id,
            title=title,
            parent_id=parent_id,
            summary=summary,
            position=position,
            depth=depth,
        )
        .returning(*project_topics.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return _from_row(result.mappings().one())


async def get_by_id(session: AsyncSession, topic_id: UUID) -> ProjectTopicRecord | None:
    stmt = select(project_topics).where(project_topics.c.id == topic_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def list_by_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ProjectTopicRecord]:
    stmt = select(project_topics).where(project_topics.c.project_id == project_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=project_topics, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
