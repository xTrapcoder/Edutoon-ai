"""Clerk authentication - JWT verification against Clerk's published JWKS.

The only module allowed to know about Clerk's token format or endpoints
(rule 3). Does exactly one thing: turn a bearer token into verified claims,
or raise. No database access, no user provisioning, no business logic -
that lives in ``services/auth.py``.

Assumes the Clerk JWT template includes an ``email`` claim (a one-line
Clerk dashboard setting) - Clerk's default session token does not carry it,
and this module has no second channel (e.g. Clerk's Backend API) to fetch
it, since that would pull in more than authentication infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

from edutoon.core.config import get_settings

_REQUIRED_ALGORITHM = "RS256"

_jwk_client: PyJWKClient | None = None


class ClerkTokenError(Exception):
    """The token is missing, malformed, expired, or fails verification."""


@dataclass(frozen=True, slots=True)
class ClerkClaims:
    clerk_user_id: str
    email: str


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(get_settings().CLERK_JWKS_URL)
    return _jwk_client


def get_signing_key(token: str) -> Any:
    """Resolve the public key that signed ``token``, via Clerk's JWKS.

    Kept as its own function (rather than inlined into :func:`verify_token`)
    so tests can substitute a fixed test key instead of fetching Clerk's
    real JWKS over the network.
    """
    try:
        return _get_jwk_client().get_signing_key_from_jwt(token).key
    except jwt.PyJWTError as exc:
        # Covers both a JWKS/key-matching failure (PyJWKClientError) and a
        # token too malformed to even parse a header from (DecodeError) -
        # both are "this token is invalid", not a server-side failure.
        raise ClerkTokenError(str(exc)) from exc


def verify_token(token: str) -> ClerkClaims:
    """Verify ``token`` and return the identity it asserts.

    Raises :class:`ClerkTokenError` for anything invalid: unknown/rotated
    signing key, bad signature, wrong issuer, expiry, or missing required
    claims. Callers (``api/dependencies.py::get_current_user``) translate
    that into :class:`~edutoon.core.errors.UnauthorizedError`.
    """
    settings = get_settings()
    signing_key = get_signing_key(token)
    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=[_REQUIRED_ALGORITHM],
            issuer=settings.CLERK_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise ClerkTokenError(str(exc)) from exc

    clerk_user_id = payload.get("sub")
    email = payload.get("email")
    if not clerk_user_id or not email:
        raise ClerkTokenError(
            "Token is missing required claims ('sub' and/or 'email')."
        )

    return ClerkClaims(clerk_user_id=clerk_user_id, email=email)
