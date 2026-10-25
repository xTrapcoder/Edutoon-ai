from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.context import get_request_id
from edutoon.core.errors import NotInvitedError
from edutoon.domain import roles as roles_domain
from edutoon.providers.clerk import ClerkClaims
from edutoon.repositories import invitations as invitations_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.users import UserRecord
from edutoon.services.audit_logs import record_audit_log

NOT_INVITED_MESSAGE = "This account has not been invited. Ask an administrator for an invitation."


async def resolve_user(session: AsyncSession, claims: ClerkClaims) -> UserRecord:
    """Resolve a verified Clerk identity to a ``users`` row - and *only* to one that is allowed
    to exist.

    1. A user already linked to this Clerk identity is returned as it is (its ``status`` is the
       caller's concern: ``get_current_user`` refuses a suspended one).
    2. Otherwise the identity is new. It becomes a user only if its address is **verified** and
       either a pending, unexpired invitation names that address (the user gets the invitation's
       role) or it is the first administrator named by ``ADMIN_BOOTSTRAP_EMAILS``.
    3. Anything else raises :class:`NotInvitedError` (403) and **writes nothing**.

    The role comes from the invitation or the operator's configuration - never from the token.
    """
    existing = await users_repo.get_by_clerk_user_id(session, claims.clerk_user_id)
    if existing is not None:
        return existing

    if not claims.email_verified:
        raise NotInvitedError(NOT_INVITED_MESSAGE)
    email = roles_domain.normalise_email(claims.email)

    invitation = await invitations_repo.find_usable_for_email_for_update(session, email)
    if invitation is not None:
        # Re-read after taking the lock: a concurrent first request may have just accepted it.
        existing = await users_repo.get_by_clerk_user_id(session, claims.clerk_user_id)
        if existing is not None:
            return existing
        user = await users_repo.create(
            session,
            email=email,
            role=invitation.role,
            clerk_user_id=claims.clerk_user_id,
            display_name=invitation.display_name,
        )
        await invitations_repo.mark_accepted(session, invitation.id, user_id=user.id)
        await record_audit_log(
            session,
            action="user.provisioned",
            entity_type="user",
            actor_id=user.id,
            entity_id=user.id,
            request_id=get_request_id(),
            context={"via": "invitation", "invitation_id": str(invitation.id), "role": user.role},
        )
        return user

    if roles_domain.may_bootstrap_first_admin(
        email=email,
        email_verified=claims.email_verified,
        bootstrap_emails=roles_domain.parse_email_list(get_settings().ADMIN_BOOTSTRAP_EMAILS),
        active_admin_exists=await users_repo.active_admin_exists(session),
    ):
        user = await users_repo.create(
            session,
            email=email,
            role=roles_domain.ROLE_ADMIN,
            clerk_user_id=claims.clerk_user_id,
        )
        await record_audit_log(
            session,
            action="user.provisioned",
            entity_type="user",
            actor_id=user.id,
            entity_id=user.id,
            request_id=get_request_id(),
            context={"via": "bootstrap", "role": user.role},
        )
        return user

    raise NotInvitedError(NOT_INVITED_MESSAGE)
