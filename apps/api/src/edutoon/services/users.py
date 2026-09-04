from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import Page
from edutoon.repositories import users as users_repo
from edutoon.repositories.users import UserRecord


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    clerk_user_id: str | None = None,
    display_name: str | None = None,
) -> UserRecord:
    return await users_repo.create(
        session, email=email, clerk_user_id=clerk_user_id, display_name=display_name
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
