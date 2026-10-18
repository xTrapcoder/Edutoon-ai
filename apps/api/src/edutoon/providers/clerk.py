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

import asyncio
import json
import urllib.error
import urllib.request
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
    # True only when the token carries ``email_verified: true``. Clerk's default session token has
    # no such claim; it has to be added to the JWT template. Without it an identity can still
    # sign in as a user that already exists, but can never be *provisioned* (accept an invitation
    # or become the first administrator), because the address is not proven to be theirs.
    email_verified: bool = False


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
        raise ClerkTokenError("Token is missing required claims ('sub' and/or 'email').")

    return ClerkClaims(
        clerk_user_id=clerk_user_id,
        email=email,
        email_verified=payload.get("email_verified") is True,
    )


# --- invitations (Clerk Backend API) ---------------------------------------------------------

_INVITATION_TIMEOUT_SECONDS = 10
_USER_AGENT = "EduToonAI/1.0 (clerk invitations)"


class ClerkInvitationError(Exception):
    """Clerk could not be asked to send an invitation (not configured, rejected or unreachable)."""


def invitations_configured() -> bool:
    return bool(get_settings().CLERK_SECRET_KEY)


def _post_invitation(url: str, secret_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - the scheme is fixed by configuration
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
            # Clerk's API answers the default "Python-urllib" agent with HTTP 403 even for a valid
            # key (measured 2026-10-18: the same key and endpoint gave 403 with it and 200 without),
            # so an invitation would be refused for a reason that has nothing to do with the key.
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_INVITATION_TIMEOUT_SECONDS) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Status only: the response body may echo the address, and the key is never shown.
        raise ClerkInvitationError(f"Clerk refused the invitation (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ClerkInvitationError("Clerk could not be reached to send the invitation.") from exc
    if not isinstance(body, dict):
        raise ClerkInvitationError("Clerk answered the invitation with an unexpected body.")
    return body


async def send_invitation(*, email: str) -> str:
    """Ask Clerk to email ``email`` an invitation to sign up; return Clerk's invitation id.

    Carries no role: roles live only in our database, never in anything Clerk holds. Raises
    :class:`ClerkInvitationError` if Clerk is not configured, refuses, or cannot be reached.
    UNVERIFIED against a real Clerk instance - the request shape follows Clerk's documented
    ``POST /v1/invitations`` and is exercised in tests with the network call replaced.
    """
    settings = get_settings()
    if not settings.CLERK_SECRET_KEY:
        raise ClerkInvitationError("CLERK_SECRET_KEY is not configured.")
    redirect_url = settings.CLERK_INVITATION_REDIRECT_URL or (
        settings.APP_BASE_URL.rstrip("/") + "/sign-up"
    )
    body = await asyncio.to_thread(
        _post_invitation,
        settings.CLERK_API_BASE_URL.rstrip("/") + "/v1/invitations",
        settings.CLERK_SECRET_KEY,
        {"email_address": email, "redirect_url": redirect_url, "ignore_existing": True},
    )
    invitation_id = body.get("id")
    if not isinstance(invitation_id, str) or not invitation_id:
        raise ClerkInvitationError("Clerk answered the invitation without an id.")
    return invitation_id
