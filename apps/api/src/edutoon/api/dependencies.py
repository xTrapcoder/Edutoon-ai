"""FastAPI dependencies for routers (rule 2: routers only ever import from
here / services - never repositories or providers directly).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import UnauthorizedError
from edutoon.db.session import get_session
from edutoon.providers.cache import Redis
from edutoon.providers.clerk import ClerkTokenError, verify_token
from edutoon.providers.embeddings import Embeddings
from edutoon.providers.llm import LLM
from edutoon.providers.storage import Storage
from edutoon.services.auth import get_or_provision_user
from edutoon.services.users import UserRecord


def get_redis(request: Request) -> Redis:
    # Starlette's `State` has untyped attribute access - `.redis` is `Any`.
    return request.app.state.redis  # type: ignore[no-any-return]


def get_storage(request: Request) -> Storage:
    return request.app.state.storage  # type: ignore[no-any-return]


def get_embeddings(request: Request) -> Embeddings:
    return request.app.state.embeddings  # type: ignore[no-any-return]


def get_llm(request: Request) -> LLM:
    return request.app.state.llm  # type: ignore[no-any-return]


# Routers depend on these aliases rather than importing `Redis`/`Storage`/
# `Embeddings`/`LLM` from `providers.*` themselves (rule 2: providers are only
# ever reached through this dependency-injection seam, same as
# `get_current_user` below).
RedisDep = Annotated[Redis, Depends(get_redis)]
StorageDep = Annotated[Storage, Depends(get_storage)]
EmbeddingsDep = Annotated[Embeddings, Depends(get_embeddings)]
LLMDep = Annotated[LLM, Depends(get_llm)]


# The ``Idempotency-Key`` request header (rule 8), bounded on both sides:
#
# - Not empty. Every empty key from one caller to one route would share a single
#   dedupe record, so a client that forgot to fill the header in would be served
#   the first response for a day.
# - At most 255 characters (a UUID is 36). ``jobs`` has a unique index on the key,
#   and a btree entry over about 2.7 kB is refused by Postgres, which would reach
#   the caller as an unexplained 500 rather than a 422.
IDEMPOTENCY_KEY_MAX_CHARS = 255
IdempotencyKey = Annotated[str, Header(min_length=1, max_length=IDEMPOTENCY_KEY_MAX_CHARS)]


def _extract_bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise UnauthorizedError("Missing Authorization header.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Authorization header must be a Bearer token.")
    return token


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> UserRecord:
    """Resolve the caller's verified identity, provisioning on first login.

    Raises :class:`~edutoon.core.errors.UnauthorizedError` (401) for a
    missing, malformed, or invalid/expired token - handled generically by
    the ``AppError`` exception handler already registered in ``main.py``,
    no bespoke wiring needed there.
    """
    token = _extract_bearer_token(authorization)
    try:
        claims = verify_token(token)
    except ClerkTokenError as exc:
        raise UnauthorizedError("Invalid or expired authentication token.") from exc
    return await get_or_provision_user(session, claims)
