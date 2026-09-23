from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.providers.clerk import ClerkClaims
from edutoon.repositories import users as users_repo
from edutoon.repositories.users import UserRecord


async def get_or_provision_user(session: AsyncSession, claims: ClerkClaims) -> UserRecord:
    """Resolve a verified Clerk identity to a ``users`` row.

    Looks up by ``clerk_user_id`` (reusing the existing repository); creates
    the row on first sight. Identity resolution only - no business rules.
    """
    existing = await users_repo.get_by_clerk_user_id(session, claims.clerk_user_id)
    if existing is not None:
        return existing

    return await users_repo.create(
        session,
        email=claims.email.lower(),
        clerk_user_id=claims.clerk_user_id,
    )
