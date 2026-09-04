from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import Page
from edutoon.repositories import projects as projects_repo
from edutoon.repositories.projects import ProjectRecord


async def create_project(
    session: AsyncSession,
    *,
    owner_id: UUID,
    title: str,
    source_type: str,
    description: str | None = None,
    language: str = "en-GB",
) -> ProjectRecord:
    return await projects_repo.create(
        session,
        owner_id=owner_id,
        title=title,
        source_type=source_type,
        description=description,
        language=language,
    )


async def get_project_or_404(session: AsyncSession, project_id: UUID) -> ProjectRecord:
    project = await projects_repo.get_by_id(session, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")
    return project


async def list_projects_for_owner(
    session: AsyncSession, owner_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ProjectRecord]:
    return await projects_repo.list_by_owner(session, owner_id, limit=limit, cursor=cursor)
