from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import Page
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories.project_topics import ProjectTopicRecord


async def create_project_topic(
    session: AsyncSession,
    *,
    project_id: UUID,
    title: str,
    parent_id: UUID | None = None,
    summary: str | None = None,
    position: int = 0,
    depth: int = 0,
) -> ProjectTopicRecord:
    return await project_topics_repo.create(
        session,
        project_id=project_id,
        title=title,
        parent_id=parent_id,
        summary=summary,
        position=position,
        depth=depth,
    )


async def get_project_topic_or_404(session: AsyncSession, topic_id: UUID) -> ProjectTopicRecord:
    topic = await project_topics_repo.get_by_id(session, topic_id)
    if topic is None:
        raise NotFoundError(f"Project topic {topic_id} not found.")
    return topic


async def list_project_topics(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[ProjectTopicRecord]:
    return await project_topics_repo.list_by_project(
        session, project_id, limit=limit, cursor=cursor
    )
