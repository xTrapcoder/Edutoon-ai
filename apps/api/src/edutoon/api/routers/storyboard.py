"""``/v1/projects/{project_id}/scripts/{script_id}/storyboard`` - Phase 9's
Scene / Storyboard API surface: enqueue and status/progress.

Asynchronous, the same shape as script generation and narration synthesis
(SG-5 / Phase 8): ``POST /storyboard`` enqueues a ``generate_storyboard``
job and returns ``202`` with it plus the worst-case provider-call estimate
the frontend shows *before* the caller decides to generate; the caller
polls ``GET /storyboard`` for progress, per-segment scenes, and
completeness - computed fresh on every read, never copied from a job's own
historical result (a storyboard can be ``succeeded_with_failures`` at the
job level while still reporting itself incomplete).

``project_id`` ownership is checked first on every route (rule 9: a project
the caller doesn't own is a 404, never a 403), then the script's own
ownership. The enqueue route requires an ``Idempotency-Key`` (rule 8) and
dedupes through ``services/idempotency.py`` exactly like
``routers/script_audio.py``; its transaction is committed before its result
is cached (``commit=session.commit``).

Rule 2 holds here as everywhere: persistence goes through
``services.storyboard`` only - never a repository or provider import.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import IdempotencyKey, RedisDep, get_current_user
from edutoon.api.schemas.jobs import JobResponse
from edutoon.api.schemas.storyboard import (
    StoryboardEnqueueRequest,
    StoryboardEnqueueResponse,
    StoryboardStatusResponse,
)
from edutoon.db.session import get_session
from edutoon.services import idempotency as idempotency_service
from edutoon.services import storyboard as storyboard_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/projects/{project_id}/scripts/{script_id}", tags=["storyboard"])

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/storyboard", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_storyboard(
    request: Request,
    project_id: UUID,
    script_id: UUID,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
    body: StoryboardEnqueueRequest | None = None,
) -> StoryboardEnqueueResponse:
    async def _execute() -> tuple[int, Any]:
        result = await storyboard_service.enqueue_storyboard_generation(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            script_id=script_id,
            idempotency_key=idempotency_key,
        )
        response = StoryboardEnqueueResponse(
            job=JobResponse.from_record(result.job),
            estimated_provider_calls=result.estimated_provider_calls,
        )
        return status.HTTP_202_ACCEPTED, response.model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return StoryboardEnqueueResponse.model_validate(response_body)


@router.get("/storyboard")
async def get_storyboard(
    project_id: UUID,
    script_id: UUID,
    current_user: CurrentUser,
    session: Session,
) -> StoryboardStatusResponse:
    result = await storyboard_service.get_storyboard_status(
        session, project_id=project_id, owner_id=current_user.id, script_id=script_id
    )
    return StoryboardStatusResponse.from_status(result)
