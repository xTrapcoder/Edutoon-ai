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
from edutoon.services.auth import get_or_provision_user
from edutoon.services.users import UserRecord


def get_redis(request: Request) -> Redis:
    # Starlette's `State` has untyped attribute access - `.redis` is `Any`.
    return request.app.state.redis  # type: ignore[no-any-return]


# Routers depend on this alias rather than importing `Redis` from
# `providers.cache` themselves (rule 2: providers are only ever reached
# through this dependency-injection seam, same as `get_current_user` below).
RedisDep = Annotated[Redis, Depends(get_redis)]


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
