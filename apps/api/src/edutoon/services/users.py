from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.context import get_request_id
from edutoon.core.errors import ConflictError, NotFoundError
from edutoon.core.pagination import Page
from edutoon.domain import roles as roles_domain
from edutoon.repositories import users as users_repo
from edutoon.repositories.users import UserRecord as UserRecord
from edutoon.services.audit_logs import record_audit_log


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    role: str,
    clerk_user_id: str | None = None,
    display_name: str | None = None,
) -> UserRecord:
    return await users_repo.create(
        session, email=email, role=role, clerk_user_id=clerk_user_id, display_name=display_name
    )


async def get_user_or_404(session: AsyncSession, user_id: UUID) -> UserRecord:
    user = await users_repo.get_by_id(session, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    return user


async def list_users(
    session: AsyncSession, *, limit: int = 50, cursor: str | None = None
) -> Page[UserRecord]:
    return await users_repo.list_page(session, limit=limit, cursor=cursor)


async def change_user_access(
    session: AsyncSession,
    *,
    actor_id: UUID,
    user_id: UUID,
    role: str | None = None,
    status: str | None = None,
) -> UserRecord:
    """Change a user's role and/or status. Administrators only (the route's policy).

    Safeguards: a deleted account is never revived, and the organisation can never be left with
    no active administrator (the active administrators' rows are locked while this decides, so
    two concurrent demotions cannot both succeed). Setting what is already set writes nothing.
    """
    active_admins = await users_repo.lock_and_count_active_admins(session)
    target = await users_repo.get_by_id_for_update(session, user_id)
    if target is None:
        raise NotFoundError(f"User {user_id} not found.")
    if target.status == "deleted":
        raise ConflictError("A deleted account cannot be changed.")

    new_role = role if role is not None else target.role
    new_status = status if status is not None else target.status
    if (new_role, new_status) == (target.role, target.status):
        return target

    if roles_domain.would_leave_no_active_admin(
        target_is_active_admin=target.role == roles_domain.ROLE_ADMIN and target.status == "active",
        stays_active_admin=new_role == roles_domain.ROLE_ADMIN and new_status == "active",
        active_admin_count=active_admins,
    ):
        raise ConflictError("The last active administrator cannot be demoted or suspended.")

    updated = await users_repo.update_role_and_status(
        session, user_id, role=new_role, status=new_status
    )
    for field, before, after in (
        ("role", target.role, new_role),
        ("status", target.status, new_status),
    ):
        if before != after:
            await record_audit_log(
                session,
                action=f"user.{field}_changed",
                entity_type="user",
                actor_id=actor_id,
                entity_id=user_id,
                request_id=get_request_id(),
                context={"from": before, "to": after},
            )
    return updated
