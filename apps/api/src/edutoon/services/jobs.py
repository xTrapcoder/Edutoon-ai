from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import Page
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories.jobs import JobRecord


async def create_job(
    session: AsyncSession,
    *,
    project_id: UUID,
    kind: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    idempotency_key: str | None = None,
) -> JobRecord:
    return await jobs_repo.create(
        session,
        project_id=project_id,
        kind=kind,
        payload=payload,
        priority=priority,
        max_attempts=max_attempts,
        idempotency_key=idempotency_key,
    )


async def get_job_or_404(session: AsyncSession, job_id: UUID) -> JobRecord:
    job = await jobs_repo.get_by_id(session, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    return job


async def get_job_by_idempotency_key(
    session: AsyncSession, *, project_id: UUID, kind: str, idempotency_key: str
) -> JobRecord | None:
    return await jobs_repo.get_by_idempotency_key(
        session, project_id=project_id, kind=kind, idempotency_key=idempotency_key
    )


async def list_jobs_for_project(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[JobRecord]:
    return await jobs_repo.list_by_project(session, project_id, limit=limit, cursor=cursor)
