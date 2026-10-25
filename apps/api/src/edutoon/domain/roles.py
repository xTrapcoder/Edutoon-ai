"""Roles, user statuses and invitations: the vocabulary and the pure rules.

``users.role`` in Postgres is the one authoritative source of a person's role. Nothing a client
sends - a request body, a header, or a claim inside a Clerk token - ever decides it; a role is
written only by a migration backfill, the first-administrator bootstrap, an accepted invitation,
or an administrator changing it through the API.

The vocabulary mirrors the ``CHECK`` constraints of migration 0019 (``ck_users_role``,
``ck_invitations_role``, ``ck_invitations_status``); the migration stays the source of truth for
what the database accepts and these constants are how the rest of the code names those values.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

Role = Literal["admin", "employee"]
"""``admin`` creates and manages training content; ``employee`` takes it."""

ROLE_ADMIN: Final[Role] = "admin"
ROLE_EMPLOYEE: Final[Role] = "employee"
ROLES: Final[frozenset[str]] = frozenset(get_args(Role))

UserStatus = Literal["active", "suspended", "deleted"]
"""Mirrors ``ck_users_status`` (migration 0002). Only ``active`` may use the API."""

USER_STATUS_ACTIVE: Final[UserStatus] = "active"
USER_STATUS_SUSPENDED: Final[UserStatus] = "suspended"

AssignableStatus = Literal["active", "suspended"]
"""The statuses an administrator may set. ``deleted`` is reserved: a user row is never removed."""

InvitationStatus = Literal["pending", "accepted", "revoked"]
"""Expiry is not a status: an invitation is expired when ``expires_at`` has passed."""

INVITATION_PENDING: Final[InvitationStatus] = "pending"
INVITATION_ACCEPTED: Final[InvitationStatus] = "accepted"
INVITATION_REVOKED: Final[InvitationStatus] = "revoked"


def normalise_email(email: str) -> str:
    """The form an address is stored and compared in (``users`` and ``invitations`` both
    require it lower-case)."""
    return email.strip().lower()


def parse_email_list(raw: str | None) -> frozenset[str]:
    """A comma- or whitespace-separated list of addresses, normalised, blanks dropped."""
    if not raw:
        return frozenset()
    parts = raw.replace(";", ",").replace("\n", ",").replace(" ", ",").split(",")
    return frozenset(normalise_email(part) for part in parts if part.strip())


def may_bootstrap_first_admin(
    *, email: str, email_verified: bool, bootstrap_emails: frozenset[str], active_admin_exists: bool
) -> bool:
    """Whether an uninvited, unknown identity may become the organisation's first administrator.

    All three must hold: the address is on the operator-configured allow-list, the identity
    provider vouches that the person controls it, and no active administrator exists yet. The
    last condition makes the allow-list a one-time bootstrap, not a standing back door.
    """
    return email_verified and not active_admin_exists and normalise_email(email) in bootstrap_emails


def would_leave_no_active_admin(
    *, target_is_active_admin: bool, stays_active_admin: bool, active_admin_count: int
) -> bool:
    """Whether a change to one user would leave the organisation with no active administrator."""
    return target_is_active_admin and not stays_active_admin and active_admin_count <= 1
