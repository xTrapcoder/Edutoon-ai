from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import Page
from edutoon.repositories import projects as projects_repo
from edutoon.repositories.projects import ProjectRecord as ProjectRecord


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


# --- Authenticated, ownership-scoped API (rule 9: not-owned looks like missing) ---


async def get_project(session: AsyncSession, *, project_id: UUID, owner_id: UUID) -> ProjectRecord:
    project = await projects_repo.get_by_id_for_owner(session, project_id, owner_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")
    return project


async def list_projects(
    session: AsyncSession, *, owner_id: UUID, limit: int = 50, cursor: str | None = None
) -> Page[ProjectRecord]:
    return await projects_repo.list_by_owner(session, owner_id, limit=limit, cursor=cursor)


async def update_project(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID, **fields: Any
) -> ProjectRecord:
    project = await projects_repo.update(
        session, project_id=project_id, owner_id=owner_id, **fields
    )
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")
    return project


async def delete_project(session: AsyncSession, *, project_id: UUID, owner_id: UUID) -> None:
    deleted = await projects_repo.delete(session, project_id=project_id, owner_id=owner_id)
    if not deleted:
        raise NotFoundError(f"Project {project_id} not found.")
