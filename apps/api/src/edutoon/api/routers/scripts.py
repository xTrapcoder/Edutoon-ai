"""``/v1/projects/{project_id}/scripts`` - the Script Generation Engine's
API surface (SG-5).

Generation is asynchronous: ``POST`` enqueues a ``generate_script`` job and
returns ``202`` with the freshly created ``pending`` script. The ``worker``
process runs the job (``services.scripts.run_generate_script_job``); the
caller polls ``GET /{script_id}`` until the script leaves ``pending`` -
``draft`` when the narration is ready to review, ``failed`` if generation
could not produce a fully sourced script. Approving a reviewed draft is
``POST /{script_id}/approval``.

``project_id`` ownership is checked first on every route (rule 9: a project
the caller doesn't own is a 404, never a 403), and again for the nested
``script_id`` on the detail and approval routes. Both mutating routes require
an ``Idempotency-Key`` (rule 8; one to 255 characters, otherwise 422) and dedupe
through ``services/idempotency.py`` exactly like ``routers/projects.py``: a
repeat of the same (caller, method, path, key) replays the original response
instead of queuing a second generation or re-running the approval. Each
handler's transaction is committed before its result is cached or returned
(``commit=session.commit``): the session dependency would otherwise commit only
after the response has been sent, and a failed commit would leave a cached
"queued" or "approved" for something that was never stored.

Rule 2 holds here as everywhere: this module reaches Redis only through the
``api.dependencies`` injection seam and all persistence goes through
``services.scripts`` - never a repository or provider import.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import IdempotencyKey, RedisDep, get_current_user
from edutoon.api.schemas.scripts import (
    ScriptDetailResponse,
    ScriptGenerateRequest,
    ScriptListResponse,
    ScriptResponse,
)
from edutoon.core.pagination import DEFAULT_PAGE_SIZE
from edutoon.db.session import get_session
from edutoon.services import idempotency as idempotency_service
from edutoon.services import projects as projects_service
from edutoon.services import scripts as scripts_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/projects/{project_id}/scripts", tags=["scripts"])

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_script_generation(
    request: Request,
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
    body: ScriptGenerateRequest | None = None,
) -> ScriptResponse:
    async def _execute() -> tuple[int, Any]:
        # Ownership check, the "has something to say" check and the
        # supersede + insert all live inside ``enqueue_script_generation``,
        # so running it inside the idempotency-guarded body means only the
        # lock winner ever touches the session (rule 9: a non-owned project
        # is a 404 raised in here, and a genuine concurrent duplicate is a
        # clean 409 rather than two coroutines racing the connection).
        result = await scripts_service.enqueue_script_generation(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            idempotency_key=idempotency_key,
        )
        return status.HTTP_202_ACCEPTED, ScriptResponse.from_record(
            result.script
        ).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return ScriptResponse.model_validate(response_body)


@router.get("")
async def list_scripts(
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> ScriptListResponse:
    await projects_service.get_project(
        session, project_id=project_id, owner_id=current_user.id
    )

    page = await scripts_service.list_scripts(
        session, project_id=project_id, limit=limit, cursor=cursor
    )
    return ScriptListResponse(
        items=[ScriptResponse.from_record(script) for script in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{script_id}")
async def get_script(
    project_id: UUID,
    script_id: UUID,
    current_user: CurrentUser,
    session: Session,
) -> ScriptDetailResponse:
    """A script with its segments and, per segment, the verified claims its
    narration was allowed to draw on. Two-level ownership check (rule 9):
    the project must belong to the caller and the script must belong to that
    project - either mismatch is a 404, never a 403.
    """
    detail = await scripts_service.assemble_script_detail(
        session,
        project_id=project_id,
        owner_id=current_user.id,
        script_id=script_id,
    )
    return ScriptDetailResponse.from_detail(detail)


@router.post("/{script_id}/approval", status_code=status.HTTP_200_OK)
async def approve_script(
    request: Request,
    project_id: UUID,
    script_id: UUID,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> ScriptResponse:
    async def _execute() -> tuple[int, Any]:
        script = await scripts_service.approve_script(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            script_id=script_id,
        )
        return status.HTTP_200_OK, ScriptResponse.from_record(script).model_dump(
            mode="json"
        )

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return ScriptResponse.model_validate(response_body)
