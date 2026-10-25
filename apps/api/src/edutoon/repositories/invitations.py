from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import raise_conflict_from_integrity_error
from edutoon.core.pagination import Page, paginate_rows
from edutoon.db.tables import invitations


@dataclass(frozen=True, slots=True)
class InvitationRecord:
    id: UUID
    email: str
    display_name: str | None
    role: str
    status: str
    invited_by: UUID
    expires_at: datetime
    accepted_user_id: UUID | None
    accepted_at: datetime | None
    clerk_invitation_id: str | None
    created_at: datetime
    updated_at: datetime


def _from_row(row: RowMapping) -> InvitationRecord:
    return InvitationRecord(
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        status=row["status"],
        invited_by=row["invited_by"],
        expires_at=row["expires_at"],
        accepted_user_id=row["accepted_user_id"],
        accepted_at=row["accepted_at"],
        clerk_invitation_id=row["clerk_invitation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    email: str,
    role: str,
    invited_by: UUID,
    ttl_seconds: int,
    display_name: str | None = None,
) -> InvitationRecord:
    """Insert a pending invitation that expires ``ttl_seconds`` after the database's own clock."""
    stmt = (
        invitations.insert()
        .values(
            email=email,
            role=role,
            invited_by=invited_by,
            display_name=display_name,
            expires_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds),
        )
        .returning(*invitations.c)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as exc:
        raise_conflict_from_integrity_error(exc)
    return _from_row(result.mappings().one())


async def get_by_id(session: AsyncSession, invitation_id: UUID) -> InvitationRecord | None:
    stmt = select(invitations).where(invitations.c.id == invitation_id)
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def get_by_id_for_update(
    session: AsyncSession, invitation_id: UUID
) -> InvitationRecord | None:
    stmt = select(invitations).where(invitations.c.id == invitation_id).with_for_update()
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def find_usable_for_email_for_update(
    session: AsyncSession, email: str
) -> InvitationRecord | None:
    """The pending invitation for ``email`` that has not expired, row-locked so two concurrent
    first sign-ins cannot both accept it."""
    stmt = (
        select(invitations)
        .where(
            invitations.c.email == email,
            invitations.c.status == "pending",
            invitations.c.expires_at > func.now(),
        )
        .with_for_update()
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def revoke_expired_pending_for_email(session: AsyncSession, email: str) -> None:
    """Tidy up before a new invitation: an expired invitation still holds the one-pending-per-
    address slot, so it is marked revoked."""
    await session.execute(
        invitations.update()
        .where(
            invitations.c.email == email,
            invitations.c.status == "pending",
            invitations.c.expires_at <= func.now(),
        )
        .values(status="revoked")
    )


async def mark_accepted(
    session: AsyncSession, invitation_id: UUID, *, user_id: UUID
) -> InvitationRecord:
    stmt = (
        invitations.update()
        .where(and_(invitations.c.id == invitation_id, invitations.c.status == "pending"))
        .values(status="accepted", accepted_user_id=user_id, accepted_at=func.now())
        .returning(*invitations.c)
    )
    return _from_row((await session.execute(stmt)).mappings().one())


async def mark_revoked(session: AsyncSession, invitation_id: UUID) -> InvitationRecord | None:
    """Revoke a *pending* invitation; ``None`` if it was not pending."""
    stmt = (
        invitations.update()
        .where(and_(invitations.c.id == invitation_id, invitations.c.status == "pending"))
        .values(status="revoked")
        .returning(*invitations.c)
    )
    row = (await session.execute(stmt)).mappings().one_or_none()
    return _from_row(row) if row is not None else None


async def set_clerk_invitation_id(
    session: AsyncSession, invitation_id: UUID, clerk_invitation_id: str
) -> None:
    await session.execute(
        invitations.update()
        .where(invitations.c.id == invitation_id)
        .values(clerk_invitation_id=clerk_invitation_id)
    )


async def list_page(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page[InvitationRecord]:
    stmt = select(invitations)
    if status is not None:
        stmt = stmt.where(invitations.c.status == status)
    rows, next_cursor = await paginate_rows(
        session, stmt, table=invitations, limit=limit, cursor=cursor
    )
    return Page(items=[_from_row(row) for row in rows], next_cursor=next_cursor)
