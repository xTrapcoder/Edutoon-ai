"""``/v1/projects/{project_id}/jobs`` - read-only job progress (Phase G).

Lists a project's jobs, newest first (keyset-paginated), so a caller can
poll the status of whatever background work is in flight (indexing, script
generation, ...) without a dedicated status route per resource kind.
``project_id`` ownership is checked first (rule 9: a project the caller
doesn't own is a 404, never a 403). Every route here is read-only, so none
of them need an ``Idempotency-Key`` (rule 8 only applies to mutations).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import get_current_user
from edutoon.api.schemas.jobs import JobListResponse, JobResponse
from edutoon.core.pagination import DEFAULT_PAGE_SIZE
from edutoon.db.session import get_session
from edutoon.services import jobs as jobs_service
from edutoon.services import projects as projects_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/projects/{project_id}/jobs", tags=["jobs"])

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("")
async def list_jobs(
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> JobListResponse:
    await projects_service.get_project(session, project_id=project_id, owner_id=current_user.id)

    page = await jobs_service.list_jobs_for_project(
        session, project_id, limit=limit, cursor=cursor
    )
    return JobListResponse(
        items=[JobResponse.from_record(job) for job in page.items],
        next_cursor=page.next_cursor,
    )
