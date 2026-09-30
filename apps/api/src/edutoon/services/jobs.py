from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import Page
from edutoon.domain.llm_usage import UsageReport
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


async def usage_for_project(session: AsyncSession, project_id: UUID) -> UsageReport:
    """What the language model has cost a project so far: the ``usage`` every job recorded on its
    result (``domain.llm_usage``), summed by kind of call - failed jobs included, since they were
    paid for. A claim typed in by hand has no job and is not in it (it is in the ``llm.call``
    log line only). Ownership is the caller's to check (rule 9)."""
    report = UsageReport()
    for result in await jobs_repo.list_results_by_project(session, project_id):
        report.merge(UsageReport.from_dict(result.get("usage")))
    return report


async def claim_next_job(session: AsyncSession) -> JobRecord | None:
    """Claim the next due queued job for a worker (see
    ``repositories.jobs.claim_next`` for the concurrency-safety details).
    """
    return await jobs_repo.claim_next(session)
