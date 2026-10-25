"""Make an EXISTING user an administrator. An operator tool - run it on the server, by hand.

    uv run python scripts/grant_admin.py --email someone@company.example

Why it exists: migration 0019 makes every existing user an employee unless their address was on
``ADMIN_BOOTSTRAP_EMAILS`` when it ran. This is the deliberate, explicit way to promote someone
afterwards (or to recover when the only administrator was lost) without opening the API to
anything: it talks to the database directly, so it needs the same ``DATABASE_URL`` the API has, and
the change is written to ``audit_logs`` as a ``system`` action.

It only promotes a user who already exists and is active. Someone who has never signed in is
brought in by an invitation (``POST /v1/admin/invitations``) or, for the very first administrator,
``ADMIN_BOOTSTRAP_EMAILS``. It never demotes anyone; use ``PATCH /v1/admin/users/{id}``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from edutoon.db.database import dispose_engine
from edutoon.db.session import get_sessionmaker
from edutoon.domain.roles import normalise_email
from edutoon.repositories import users as users_repo
from edutoon.services.audit_logs import record_audit_log


async def grant(email: str) -> int:
    email = normalise_email(email)
    async with get_sessionmaker()() as session:
        user = await users_repo.get_by_email(session, email)
        if user is None:
            print(f"No user with the address {email}. They must sign in once after an invitation.")
            return 1
        if user.status != "active":
            print(f"{email} is {user.status}; reinstate the account first.")
            return 1
        if user.role == "admin":
            print(f"{email} is already an administrator. Nothing changed.")
            return 0
        await users_repo.update_role_and_status(session, user.id, role="admin", status=user.status)
        await record_audit_log(
            session,
            action="user.role_changed",
            entity_type="user",
            actor_type="system",
            entity_id=user.id,
            context={"from": user.role, "to": "admin", "via": "scripts/grant_admin.py"},
        )
        await session.commit()
        print(f"{email} is now an administrator.")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Make an existing user an administrator.")
    parser.add_argument("--email", required=True, help="the user's email address")
    args = parser.parse_args()

    async def run() -> int:
        try:
            return await grant(args.email)
        finally:
            await dispose_engine()

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
