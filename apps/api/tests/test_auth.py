"""Authentication infrastructure tests. No real Clerk account is used or
needed anywhere here: tokens are signed with a locally-generated RSA
keypair, and `providers.clerk.get_signing_key` (the one seam that would
otherwise fetch Clerk's real JWKS over the network) is monkeypatched to
return that keypair's public key instead.
"""

from __future__ import annotations

import time
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fastapi import FastAPI

from edutoon.db.session import get_session
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.clerk import ClerkClaims
from edutoon.services import auth as auth_service

TEST_ISSUER = "https://test.clerk.accounts.dev"


def _generate_rsa_keypair() -> tuple[bytes, RSAPublicKey]:
    private_key: RSAPrivateKey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, private_key.public_key()


def _make_token(
    private_pem: bytes,
    *,
    sub: str = "clerk_user_123",
    email: str | None = "person@example.com",
    issuer: str = TEST_ISSUER,
    expires_in: int = 3600,
) -> str:
    now = int(time.time())
    payload: dict[str, object] = {"sub": sub, "iat": now, "exp": now + expires_in, "iss": issuer}
    if email is not None:
        payload["email"] = email
    return jwt.encode(payload, private_pem, algorithm="RS256")


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[bytes, RSAPublicKey]:
    return _generate_rsa_keypair()


def _build_app_with_session_override(session: object) -> FastAPI:
    app = create_app()

    async def _session_override():  # type: ignore[no-untyped-def]
        yield session

    app.dependency_overrides[get_session] = _session_override
    return app


# --- providers/clerk.py: token verification -----------------------------------


def test_verify_token_returns_claims_for_a_valid_token(monkeypatch, rsa_keypair):
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)
    token = _make_token(private_pem, sub="clerk_abc", email="ada@example.com")

    claims = clerk.verify_token(token)

    assert claims == ClerkClaims(clerk_user_id="clerk_abc", email="ada@example.com")


def test_verify_token_rejects_an_expired_token(monkeypatch, rsa_keypair):
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)
    token = _make_token(private_pem, expires_in=-3600)

    with pytest.raises(clerk.ClerkTokenError):
        clerk.verify_token(token)


def test_verify_token_rejects_a_token_signed_by_a_different_key(monkeypatch, rsa_keypair):
    _, public_key = rsa_keypair
    other_private_pem, _ = _generate_rsa_keypair()
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)
    token = _make_token(other_private_pem)

    with pytest.raises(clerk.ClerkTokenError):
        clerk.verify_token(token)


def test_verify_token_rejects_wrong_issuer(monkeypatch, rsa_keypair):
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)
    token = _make_token(private_pem, issuer="https://someone-else.clerk.accounts.dev")

    with pytest.raises(clerk.ClerkTokenError):
        clerk.verify_token(token)


def test_verify_token_rejects_a_token_missing_the_email_claim(monkeypatch, rsa_keypair):
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)
    token = _make_token(private_pem, email=None)

    with pytest.raises(clerk.ClerkTokenError):
        clerk.verify_token(token)


def test_verify_token_rejects_a_malformed_token(monkeypatch, rsa_keypair):
    _, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)

    with pytest.raises(clerk.ClerkTokenError):
        clerk.verify_token("not-a-real-jwt")


# --- services/auth.py: provisioning ---------------------------------------------


async def test_first_login_provisions_a_new_user(db_session):
    claims = ClerkClaims(clerk_user_id=f"clerk_{uuid4()}", email="new.user@example.com")

    user = await auth_service.get_or_provision_user(db_session, claims)

    assert user.clerk_user_id == claims.clerk_user_id
    assert user.email == "new.user@example.com"


async def test_existing_user_is_reused_not_duplicated(db_session):
    claims = ClerkClaims(clerk_user_id=f"clerk_{uuid4()}", email="same.user@example.com")

    first = await auth_service.get_or_provision_user(db_session, claims)
    second = await auth_service.get_or_provision_user(db_session, claims)

    assert first.id == second.id


async def test_provisioned_email_is_normalised_to_lowercase(db_session):
    claims = ClerkClaims(clerk_user_id=f"clerk_{uuid4()}", email="Mixed.Case@Example.com")

    user = await auth_service.get_or_provision_user(db_session, claims)

    assert user.email == "mixed.case@example.com"


# --- api/dependencies.py + /v1/auth/me: end-to-end over real HTTP --------------


async def test_me_resolves_identity_for_a_valid_token_and_reuses_it_on_a_second_call(
    monkeypatch, rsa_keypair, db_session
):
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)
    token = _make_token(
        private_pem, sub=f"clerk_{uuid4()}", email="whoami@example.com"
    )
    app = _build_app_with_session_override(db_session)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        second = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert first.status_code == 200
    first_body = first.json()
    assert first_body["email"] == "whoami@example.com"
    # Same identity, second request - must resolve to the same user, not a duplicate.
    assert second.json()["id"] == first_body["id"]


async def test_me_returns_401_when_authorization_header_is_missing(db_session):
    app = _build_app_with_session_override(db_session)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_me_returns_401_when_header_is_not_a_bearer_token(db_session):
    app = _build_app_with_session_override(db_session)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/auth/me", headers={"Authorization": "Basic abc123"})

    assert response.status_code == 401


async def test_me_returns_401_for_an_invalid_token(db_session):
    app = _build_app_with_session_override(db_session)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"}
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_me_returns_401_for_an_expired_token(monkeypatch, rsa_keypair, db_session):
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)
    token = _make_token(private_pem, expires_in=-3600)
    app = _build_app_with_session_override(db_session)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
