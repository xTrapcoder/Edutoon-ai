"""``/v1/admin`` - users and invitations. Administrators only.

Every mutating route requires an ``Idempotency-Key`` (rule 8) and is deduplicated against it, with
the transaction committed before the result is cached, exactly as in ``routers/projects.py``.
Users and invitations belong to the one organisation, so an administrator sees them all; an id
that does not exist is a 404.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import IdempotencyKey, RedisDep
from edutoon.api.policies import require_admin
from edutoon.api.schemas.admin_users import (
    InvitationCreatedResponse,
    InvitationCreateRequest,
    InvitationListResponse,
    InvitationResponse,
    UserListResponse,
    UserResponse,
    UserUpdateRequest,
)
from edutoon.core.pagination import DEFAULT_PAGE_SIZE
from edutoon.db.session import get_session
from edutoon.domain.roles import InvitationStatus
from edutoon.services import idempotency as idempotency_service
from edutoon.services import invitations as invitations_service
from edutoon.services import users as users_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

Admin = Annotated[UserRecord, Depends(require_admin)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    request: Request,
    body: InvitationCreateRequest,
    admin: Admin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> InvitationCreatedResponse:
    async def _execute() -> tuple[int, Any]:
        created = await invitations_service.create_invitation(
            session,
            actor_id=admin.id,
            email=body.email,
            role=body.role,
            display_name=body.display_name,
        )
        response = InvitationCreatedResponse.from_created(created)
        return status.HTTP_201_CREATED, response.model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return InvitationCreatedResponse.model_validate(response_body)


@router.get("/invitations")
async def list_invitations(
    session: Session,
    invitation_status: Annotated[InvitationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query()] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> InvitationListResponse:
    page = await invitations_service.list_invitations(
        session, status=invitation_status, limit=limit, cursor=cursor
    )
    return InvitationListResponse(
        items=[InvitationResponse.from_record(record) for record in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/invitations/{invitation_id}/revoke")
async def revoke_invitation(
    request: Request,
    invitation_id: UUID,
    admin: Admin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> InvitationResponse:
    async def _execute() -> tuple[int, Any]:
        record = await invitations_service.revoke_invitation(
            session, actor_id=admin.id, invitation_id=invitation_id
        )
        return status.HTTP_200_OK, InvitationResponse.from_record(record).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return InvitationResponse.model_validate(response_body)


@router.get("/users")
async def list_users(
    session: Session,
    limit: Annotated[int, Query()] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> UserListResponse:
    page = await users_service.list_users(session, limit=limit, cursor=cursor)
    return UserListResponse(
        items=[UserResponse.from_record(user) for user in page.items],
        next_cursor=page.next_cursor,
    )


@router.patch("/users/{user_id}")
async def update_user(
    request: Request,
    user_id: UUID,
    body: UserUpdateRequest,
    admin: Admin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> UserResponse:
    async def _execute() -> tuple[int, Any]:
        user = await users_service.change_user_access(
            session, actor_id=admin.id, user_id=user_id, role=body.role, status=body.status
        )
        return status.HTTP_200_OK, UserResponse.from_record(user).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="PATCH",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return UserResponse.model_validate(response_body)
