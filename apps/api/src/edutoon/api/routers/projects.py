"""``/v1/projects`` - the first authenticated, ownership-scoped resource API.

Every mutating route (POST/PATCH/DELETE) requires an ``Idempotency-Key``
header (rule 8) and actually deduplicates against it via
``services/idempotency.py``: a repeat of the same (caller, method, path, key)
replays the original response instead of re-running the handler, and a
genuine concurrent duplicate gets 409.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import RedisDep, get_current_user
from edutoon.api.schemas.projects import (
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdateRequest,
)
from edutoon.core.pagination import DEFAULT_PAGE_SIZE
from edutoon.db.session import get_session
from edutoon.services import idempotency as idempotency_service
from edutoon.services import projects as projects_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/projects", tags=["projects"])

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]
IdempotencyKey = Annotated[str, Header()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(
    request: Request,
    body: ProjectCreateRequest,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> ProjectResponse:
    async def _execute() -> tuple[int, Any]:
        project = await projects_service.create_project(
            session,
            owner_id=current_user.id,
            title=body.title,
            source_type=body.source_type,
            description=body.description,
            language=body.language,
        )
        return status.HTTP_201_CREATED, ProjectResponse.from_record(project).model_dump(
            mode="json"
        )

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
    )
    return ProjectResponse.model_validate(response_body)


@router.get("")
async def list_projects(
    current_user: CurrentUser,
    session: Session,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> ProjectListResponse:
    page = await projects_service.list_projects(
        session, owner_id=current_user.id, limit=limit, cursor=cursor
    )
    return ProjectListResponse(
        items=[ProjectResponse.from_record(project) for project in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{project_id}")
async def get_project(
    project_id: UUID, current_user: CurrentUser, session: Session
) -> ProjectResponse:
    project = await projects_service.get_project(
        session, project_id=project_id, owner_id=current_user.id
    )
    return ProjectResponse.from_record(project)


@router.patch("/{project_id}")
async def update_project(
    request: Request,
    project_id: UUID,
    body: ProjectUpdateRequest,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> ProjectResponse:
    fields = body.model_dump(exclude_unset=True)

    async def _execute() -> tuple[int, Any]:
        project = await projects_service.update_project(
            session, project_id=project_id, owner_id=current_user.id, **fields
        )
        return status.HTTP_200_OK, ProjectResponse.from_record(project).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="PATCH",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
    )
    return ProjectResponse.model_validate(response_body)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    request: Request,
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> None:
    async def _execute() -> tuple[int, Any]:
        await projects_service.delete_project(
            session, project_id=project_id, owner_id=current_user.id
        )
        return status.HTTP_204_NO_CONTENT, None

    await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="DELETE",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
    )
