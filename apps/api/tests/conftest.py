"""Test configuration.

Populates the required environment variables before the application is
imported so that settings resolution succeeds without a real ``.env`` file.
"""

from __future__ import annotations

import os

_TEST_ENV = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+asyncpg://edutoon:edutoon@localhost:5433/edutoon",
    "DATABASE_DIRECT_URL": "postgresql+asyncpg://edutoon:edutoon@localhost:5433/edutoon",
    "REDIS_URL": "redis://localhost:6379/0",
    "STORAGE_ENDPOINT_URL": "http://localhost:9000",
    "STORAGE_ACCESS_KEY_ID": "edutoon",
    "STORAGE_SECRET_ACCESS_KEY": "edutoon123",
    "BUCKET_UPLOADS": "edutoon-uploads",
    "BUCKET_ASSETS": "edutoon-assets",
    "BUCKET_SEGMENTS": "edutoon-segments",
    "BUCKET_OUTPUTS": "edutoon-outputs",
    "CLERK_JWKS_URL": "https://test.clerk.accounts.dev/.well-known/jwks.json",
    "CLERK_ISSUER": "https://test.clerk.accounts.dev",
    # Never a real key - every test that touches the OpenAI client
    # monkeypatches its network call, so this only needs to satisfy
    # Settings/AsyncOpenAI's constructor, never actually authenticate.
    "OPENAI_API_KEY": "sk-test-not-a-real-key",
    "ANTHROPIC_API_KEY": "sk-ant-test-not-a-real-key",
    # The evidence-per-minute guard has its own tests (test_evidence_sufficiency.py), which set
    # the real default. The rest of the suite builds the smallest possible project - one claim, the
    # default five-minute target - to test something else, so here a claim may carry an hour.
    "EVIDENCE_SECONDS_PER_CLAIM": "3600",
    # Keyword-only projects are off in production (test_keyword_only_disabled.py holds that). Many
    # older tests build a project the quickest way - source_type "topic" - to test something else,
    # so they run with the mode on; the tests about the mode being off turn it off again.
    "KEYWORD_ONLY_PROJECTS_ENABLED": "true",
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)

import time  # noqa: E402
from collections.abc import AsyncIterator, Callable  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from edutoon.core.config import get_settings  # noqa: E402
from edutoon.db.session import get_session  # noqa: E402
from edutoon.main import create_app  # noqa: E402
from edutoon.providers import clerk  # noqa: E402
from edutoon.providers.cache import Redis, get_redis_client  # noqa: E402
from edutoon.providers.storage import get_storage_client  # noqa: E402
from edutoon.repositories import users as _users_repo  # noqa: E402
from tests.role_fixtures import Person  # noqa: E402


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """A session bound to a transaction that is always rolled back.

    Repositories/services may call ``session.commit()`` freely — with
    ``join_transaction_mode="create_savepoint"`` that becomes a SAVEPOINT
    release rather than an outer commit, so nothing written by a test
    persists past it.
    """
    engine = create_async_engine(get_settings().DATABASE_DIRECT_URL)
    async with engine.connect() as conn:
        trans = await conn.begin()
        sessionmaker = async_sessionmaker(
            bind=conn,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        async with sessionmaker() as session:
            yield session
        await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """The local Redis the idempotency dedupe records live in, on its own connection."""
    client = get_redis_client(get_settings().REDIS_URL)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _legacy_roles_and_provisioning(request, monkeypatch):
    """Keep the tests written before roles existed working, without editing each of them.

    Production has **no default role** and provisions **nobody** automatically (migration 0019,
    ``services.auth.resolve_user``). A great many older tests build users straight through
    ``users_repo.create(...)`` and sign in with a freshly minted Clerk identity, both of which
    assumed "any user is the owner of their projects". For those, and only in tests:

    * ``users_repo.create`` defaults ``role`` to ``admin`` (the role that owns projects), and
    * an unknown Clerk identity is provisioned on first sight as an active administrator.

    A test of the real behaviour - roles, invitations, the uninvited 403, suspended accounts -
    carries ``@pytest.mark.real_auth`` (or ``pytestmark = pytest.mark.real_auth``) and gets
    neither; there it must give every user an explicit role. The status check in
    ``get_current_user`` is never shimmed.
    """
    if request.node.get_closest_marker("real_auth") is not None:
        return

    import edutoon.api.dependencies as dependencies

    real_create = _users_repo.create

    async def create_defaulting_to_admin(session, **kwargs):
        kwargs.setdefault("role", "admin")
        return await real_create(session, **kwargs)

    async def provision_as_admin(session, claims):
        existing = await _users_repo.get_by_clerk_user_id(session, claims.clerk_user_id)
        if existing is not None:
            return existing
        return await real_create(
            session,
            email=claims.email.lower(),
            role="admin",
            clerk_user_id=claims.clerk_user_id,
        )

    monkeypatch.setattr(_users_repo, "create", create_defaulting_to_admin)
    monkeypatch.setattr(dependencies, "resolve_user", provision_as_admin)


# --- fixtures for the tests of roles, invitations and suspended accounts ---------------------
# (helpers and the Person type: tests/role_fixtures.py)

TEST_ISSUER = "https://test.clerk.accounts.dev"


@pytest.fixture(scope="module")
def role_keypair() -> tuple[bytes, RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, private_key.public_key()


@pytest.fixture
def mint_token(role_keypair, monkeypatch) -> Callable[..., str]:
    """``mint_token(sub=..., email=..., email_verified=True, **extra_claims)`` -> a signed JWT."""
    private_pem, public_key = role_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)

    def _mint(
        *,
        sub: str | None = None,
        email: str | None = None,
        email_verified: bool | None = True,
        **extra_claims: object,
    ) -> str:
        now = int(time.time())
        payload: dict[str, object] = {
            "sub": sub or f"clerk_{uuid4()}",
            "email": email or f"{uuid4()}@example.com",
            "iat": now,
            "exp": now + 3600,
            "iss": TEST_ISSUER,
            **extra_claims,
        }
        if email_verified is not None:
            payload["email_verified"] = email_verified
        return jwt.encode(payload, private_pem, algorithm="RS256")

    return _mint


@pytest.fixture
async def role_app(db_session) -> AsyncIterator[FastAPI]:
    """The real application (all real routes and dependencies) over the rolled-back session."""
    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    # ASGITransport never runs the lifespan, so set what the idempotency dependency reads.
    settings = get_settings()
    app.state.redis = get_redis_client(settings.REDIS_URL)
    app.state.storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    yield app
    await app.state.redis.aclose()


@pytest.fixture
async def role_client(role_app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=role_app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def make_person(db_session, mint_token) -> Callable[..., object]:
    """``await make_person(role="employee", status="active")`` -> a user row plus its headers."""

    async def _make(*, role: str, status: str = "active") -> Person:
        sub = f"clerk_{uuid4()}"
        email = f"{uuid4()}@example.com"
        user = await _users_repo.create(
            db_session, email=email, role=role, clerk_user_id=sub, display_name="Test"
        )
        if status != "active":
            user = await _users_repo.update_role_and_status(
                db_session, user.id, role=role, status=status
            )
        token = mint_token(sub=sub, email=email)
        return Person(user, {"Authorization": f"Bearer {token}"}, email, sub)

    return _make


