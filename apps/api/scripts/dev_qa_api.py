"""Run the REAL API for browser QA, without a Clerk instance. DEVELOPMENT ONLY.

    uv run python scripts/dev_qa_api.py          # :8000, the real database, storage and routes

The web app's UI QA mode (``pnpm --filter @edutoon/web dev:qa:real``) stands in for Clerk and
sends a fixed bearer token. This launcher makes the API accept exactly that token (as a local
administrator) and one more for a local employee, and nothing else - so the signed-in screens
can be driven end to end against the real pipeline (Postgres, MinIO, the job worker, Piper,
FFmpeg) when a person cannot sign in through Clerk.

**This is not authentication.** It refuses to start unless ``ENVIRONMENT`` is ``development``, binds
only to ``127.0.0.1``, and changes nothing in the application: the one function that verifies a
Clerk token is replaced in this process alone. The API that ships is the one started by
``make dev`` / ``uvicorn edutoon.main:app``, which still verifies every token against Clerk.
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

import edutoon.api.dependencies as dependencies
from edutoon.core.config import get_settings
from edutoon.db.database import get_engine
from edutoon.db.session import get_sessionmaker
from edutoon.providers.clerk import ClerkClaims, ClerkTokenError
from edutoon.repositories import users as users_repo

# What ``apps/web/test-support/qa/clerk-nextjs.tsx`` returns from ``getToken()``.
QA_TOKEN = "ui-qa-token"
QA_EMPLOYEE_TOKEN = "ui-qa-employee-token"

# Two stand-in people, one per role. The launcher creates them in the local database if they are
# missing, because the application no longer provisions anyone who was not invited (migration 0019).
# The role is the database's, exactly as for a real sign-in; the token only says who is calling.
QA_USERS = {
    QA_TOKEN: (
        ClerkClaims(clerk_user_id="user_ui_qa", email="ui-qa@example.test", email_verified=True),
        "admin",
    ),
    QA_EMPLOYEE_TOKEN: (
        ClerkClaims(
            clerk_user_id="user_ui_qa_employee",
            email="ui-qa-employee@example.test",
            email_verified=True,
        ),
        "employee",
    ),
}
QA_USER = QA_USERS[QA_TOKEN][0]


def qa_verify_token(token: str) -> ClerkClaims:
    if token in QA_USERS:
        return QA_USERS[token][0]
    raise ClerkTokenError("Only the UI QA stand-in tokens are accepted by this launcher.")


async def _ensure_qa_users() -> None:
    async with get_sessionmaker()() as session:
        for claims, role in QA_USERS.values():
            if await users_repo.get_by_clerk_user_id(session, claims.clerk_user_id) is None:
                await users_repo.create(
                    session, email=claims.email, role=role, clerk_user_id=claims.clerk_user_id
                )
        await session.commit()
    # This runs in its own event loop, which asyncio.run() closes. Dispose the pool so uvicorn's
    # loop opens its own connections; a pooled one from this loop made the first request return 500.
    await get_engine().dispose()


def main() -> None:
    settings = get_settings()
    if settings.ENVIRONMENT != "development":
        sys.exit("dev_qa_api.py only runs with ENVIRONMENT=development.")
    dependencies.verify_token = qa_verify_token
    asyncio.run(_ensure_qa_users())
    print(
        "UI QA API: accepting the stand-in tokens for",
        ", ".join(claims.email for claims, _ in QA_USERS.values()),
        "- development only.",
    )
    uvicorn.run("edutoon.main:app", host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
