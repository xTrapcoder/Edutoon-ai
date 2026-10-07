"""Run the REAL API for browser QA, without a Clerk instance. DEVELOPMENT ONLY.

    uv run python scripts/dev_qa_api.py          # :8000, the real database, storage and routes

The web app's UI QA mode (``pnpm --filter @edutoon/web dev:qa:real``) stands in for Clerk and sends a
fixed bearer token. This launcher makes the API accept exactly that token, as one fixed local user,
and nothing else - so the signed-in screens can be driven end to end against the real pipeline
(Postgres, MinIO, the job worker, Piper, FFmpeg) when a person cannot sign in through Clerk.

**This is not authentication.** It refuses to start unless ``ENVIRONMENT`` is ``development``, binds
only to ``127.0.0.1``, and changes nothing in the application: the one function that verifies a
Clerk token is replaced in this process alone. The API that ships is the one started by
``make dev`` / ``uvicorn edutoon.main:app``, which still verifies every token against Clerk.
"""

from __future__ import annotations

import sys

import uvicorn

import edutoon.api.dependencies as dependencies
from edutoon.core.config import get_settings
from edutoon.providers.clerk import ClerkClaims, ClerkTokenError

# What ``apps/web/test-support/qa/clerk-nextjs.tsx`` returns from ``getToken()``.
QA_TOKEN = "ui-qa-token"
QA_USER = ClerkClaims(clerk_user_id="user_ui_qa", email="ui-qa@example.test")


def qa_verify_token(token: str) -> ClerkClaims:
    if token == QA_TOKEN:
        return QA_USER
    raise ClerkTokenError("Only the UI QA stand-in token is accepted by this launcher.")


def main() -> None:
    settings = get_settings()
    if settings.ENVIRONMENT != "development":
        sys.exit("dev_qa_api.py only runs with ENVIRONMENT=development.")
    dependencies.verify_token = qa_verify_token
    print("UI QA API: accepting the stand-in token as", QA_USER.email, "- development only.")
    uvicorn.run("edutoon.main:app", host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
