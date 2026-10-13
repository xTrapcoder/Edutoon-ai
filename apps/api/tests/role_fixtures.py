"""Plain helpers for the tests of roles, invitations and suspended accounts
(``@pytest.mark.real_auth``). The fixtures they use (``mint_token``, ``role_app``, ``role_client``,
``make_person``) live in ``conftest.py``.

No real Clerk account is used: tokens are signed with a locally generated RSA key and
``providers.clerk.get_signing_key`` is replaced, exactly as in ``test_auth.py``. The database is
the rolled-back ``db_session`` transaction, so nothing a test creates survives it.

The shared development database may already hold real users - in particular an active
administrator - so a test that depends on "no administrator exists" calls
:func:`suspend_every_existing_admin` first (inside its rolled-back transaction).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import text

from edutoon.repositories.users import UserRecord


@dataclass(frozen=True)
class Person:
    user: UserRecord
    headers: dict[str, str]
    email: str
    clerk_user_id: str


async def suspend_every_existing_admin(db_session) -> None:
    """Make "no active administrator exists" true, inside the test's rolled-back transaction."""
    await db_session.execute(
        text("UPDATE users SET status = 'suspended' WHERE role = 'admin' AND status = 'active'")
    )


def idem(key: str | None = None) -> dict[str, str]:
    return {"Idempotency-Key": key or f"test-{uuid4()}"}
