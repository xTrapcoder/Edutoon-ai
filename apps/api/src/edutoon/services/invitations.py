"""Invitations: an administrator names an address and a role; that person can then sign in.

The invitation row is the authority (``services.auth.resolve_user`` accepts it on first sign-in).
Emailing it is a convenience done through Clerk when ``CLERK_SECRET_KEY`` is configured; if it is
not, or Clerk fails, the invitation still exists and the administrator can pass the sign-up link on
themselves. The role is stored here only and is never sent to Clerk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.context import get_request_id
from edutoon.core.errors import ConflictError, NotFoundError
from edutoon.core.pagination import Page
from edutoon.domain import roles as roles_domain
from edutoon.providers import clerk
from edutoon.repositories import invitations as invitations_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.invitations import InvitationRecord as InvitationRecord
from edutoon.services.audit_logs import record_audit_log

log = structlog.get_logger()

Delivery = Literal["sent", "not_configured", "failed"]


@dataclass(frozen=True, slots=True)
class InvitationCreated:
    invitation: InvitationRecord
    delivery: Delivery


async def create_invitation(
    session: AsyncSession,
    *,
    actor_id: UUID,
    email: str,
    role: str,
    display_name: str | None = None,
) -> InvitationCreated:
    email = roles_domain.normalise_email(email)
    if await users_repo.get_by_email(session, email) is not None:
        raise ConflictError("A user with this email address already exists.")

    await invitations_repo.revoke_expired_pending_for_email(session, email)
    if await invitations_repo.find_usable_for_email_for_update(session, email) is not None:
        raise ConflictError("This email address already has a pending invitation.")

    ttl_seconds = int(get_settings().INVITATION_TTL_DAYS * 24 * 60 * 60)
    invitation = await invitations_repo.create(
        session,
        email=email,
        role=role,
        invited_by=actor_id,
        ttl_seconds=ttl_seconds,
        display_name=display_name,
    )
    await record_audit_log(
        session,
        action="invitation.created",
        entity_type="invitation",
        actor_id=actor_id,
        entity_id=invitation.id,
        request_id=get_request_id(),
        context={"role": role},
    )
    # The invitation is durable before anything is sent: an email that went out for a row that was
    # then lost would leave a person invited by Clerk and refused by us.
    await session.commit()

    delivery: Delivery = "not_configured"
    if clerk.invitations_configured():
        try:
            clerk_id = await clerk.send_invitation(email=email)
        except clerk.ClerkInvitationError as exc:
            log.warning(
                "invitation.delivery_failed", invitation_id=str(invitation.id), error=str(exc)
            )
            delivery = "failed"
        else:
            await invitations_repo.set_clerk_invitation_id(session, invitation.id, clerk_id)
            delivery = "sent"
    return InvitationCreated(invitation=invitation, delivery=delivery)


async def revoke_invitation(
    session: AsyncSession, *, actor_id: UUID, invitation_id: UUID
) -> InvitationRecord:
    revoked = await invitations_repo.mark_revoked(session, invitation_id)
    if revoked is None:
        if await invitations_repo.get_by_id(session, invitation_id) is None:
            raise NotFoundError(f"Invitation {invitation_id} not found.")
        raise ConflictError("Only a pending invitation can be revoked.")
    await record_audit_log(
        session,
        action="invitation.revoked",
        entity_type="invitation",
        actor_id=actor_id,
        entity_id=invitation_id,
        request_id=get_request_id(),
        context={},
    )
    return revoked


async def list_invitations(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page[InvitationRecord]:
    return await invitations_repo.list_page(session, status=status, limit=limit, cursor=cursor)
