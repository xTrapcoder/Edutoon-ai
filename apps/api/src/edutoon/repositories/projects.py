from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
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


async def get_by_id_for_owner(
    session: AsyncSession, project_id: UUID, owner_id: UUID
) -> ProjectRecord | None:
    """Ownership-scoped fetch (rule 9): missing and not-owned look identical
    to the caller - both return ``None``, never a separate "forbidden" case.
    """
    stmt = select(projects).where(projects.c.id == project_id, projects.c.owner_id == owner_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def update(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID, **fields: Any
) -> ProjectRecord | None:
    """Ownership-scoped partial update. Returns ``None`` if the project does
    not exist or is not owned by ``owner_id`` - same rule-9 shape as
    :func:`get_by_id_for_owner`. An empty ``fields`` is a no-op read.
    """
    if not fields:
        return await get_by_id_for_owner(session, project_id, owner_id)

    stmt = (
        projects.update()
        .where(projects.c.id == project_id, projects.c.owner_id == owner_id)
        .values(**fields)
        .returning(*projects.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    row = result.mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def delete(session: AsyncSession, *, project_id: UUID, owner_id: UUID) -> bool:
    """Ownership-scoped delete. Returns ``True`` iff a row was removed."""
    stmt = projects.delete().where(projects.c.id == project_id, projects.c.owner_id == owner_id)
    result = cast(CursorResult[Any], await session.execute(stmt))
    return result.rowcount > 0


async def list_by_owner(
    session: AsyncSession, owner_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ProjectRecord]:
    stmt = select(projects).where(projects.c.owner_id == owner_id)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=projects, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
