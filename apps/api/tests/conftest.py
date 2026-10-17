"""Test configuration.

Populates the required environment variables before the application is
imported so that settings resolution succeeds without a real ``.env`` file.

**Object storage is isolated** (``tests/storage_isolation.py``): the four ``BUCKET_*`` settings are
overwritten with per-session scratch buckets before the application reads them, every storage client
refuses any other bucket, and the scratch buckets are created at session start and deleted at its
end. The development buckets are never addressed.
"""

from __future__ import annotations

import os

# Imported first: it records the original BUCKET_* values before they are replaced below.
from tests import storage_isolation

_TEST_ENV = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+asyncpg://edutoon:edutoon@localhost:5433/edutoon",
    "DATABASE_DIRECT_URL": "postgresql+asyncpg://edutoon:edutoon@localhost:5433/edutoon",
    "REDIS_URL": "redis://localhost:6379/0",
    "STORAGE_ENDPOINT_URL": "http://localhost:9000",
    "STORAGE_ACCESS_KEY_ID": "edutoon",
    "STORAGE_SECRET_ACCESS_KEY": "edutoon123",
    # BUCKET_* are deliberately absent: they are overwritten with scratch buckets just below.
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
    # The sentence-level support check makes a model call per sentence; the older script tests fake
    # only the writer, so they run without it. test_script_support_check.py turns it on.
    "SCRIPT_SUPPORT_CHECK_ENABLED": "false",
    # Older script tests are about a section that cannot be made failing the generation; the
    # claims-as-section fallback has its own tests (test_script_support_check.py), which turn it on.
    "SCRIPT_FALLBACK_TO_CLAIMS": "false",
    "STORYBOARD_FALLBACK_TO_TEXT": "false",
    "SCRIPT_FIXED_CLOSING": "false",
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)

# A test run never reads a developer's own ``.env``: it would let choices made for local work (the
# local AI backend, model names, smaller limits) change what the tests prove. Everything a test
# needs is set above or is the setting's default.
from edutoon.core.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None

# Unconditional: a bucket named in the shell or in ``.env`` must never win in a test run.
storage_isolation.point_settings_at_scratch_buckets()

import time  # noqa: E402
from collections.abc import AsyncIterator, Callable, Iterator  # noqa: E402
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

# From here every Storage built in this process refuses a bucket that is not a scratch one.
storage_isolation.install_guard()

_scratch_buckets_created = False


def pytest_sessionstart(session: pytest.Session) -> None:
    """Create this session's four scratch buckets. If storage is unreachable the session carries
    on (tests that need it fail with their own error; the pure ones still run) - the guard still
    stops anything from reaching a development bucket."""
    global _scratch_buckets_created
    try:
        storage_isolation.create_scratch_buckets()
        _scratch_buckets_created = True
    except Exception as exc:  # noqa: BLE001
        import warnings

        warnings.warn(
            f"scratch storage buckets were not created: {type(exc).__name__}: {exc}", stacklevel=2
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Empty and delete the scratch buckets this session created - and only those."""
    if not _scratch_buckets_created:
        return
    failed = storage_isolation.delete_scratch_buckets()
    if failed:
        import warnings

        warnings.warn(
            f"scratch storage buckets left behind (harmless, edutoon-test-*): {failed}",
            stacklevel=2,
        )


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


@pytest.fixture(scope="session")
def committed_database_url() -> Iterator[str]:
    """A database of its own, migrated to head, for every test that really **commits**.

    ``audit_logs`` is append-only (a trigger rejects deleting a row), so a test that commits through
    the application writes audit rows that nothing can take back. Run against the shared
    development database, such tests grew it from 301 to 1,336 audit rows. They use this database
    instead: it is created for the test session, migrated by the real ``alembic upgrade head``,
    and dropped when the session ends, so what they commit - audit rows included - disappears with
    it and the development database is not written to at all. (The ordinary ``db_session`` tests
    roll back and need none.)
    """
    from tests.migration_helpers import run_alembic, scratch_database

    with scratch_database() as url:
        run_alembic(url, "upgrade", "head")
        yield url


@pytest.fixture(autouse=True)
def _scratch_url_for_engine_modules(request) -> str | None:
    """The scratch database's URL for a test module that reaches the application's process-wide
    engine (``get_engine`` / ``get_sessionmaker``), else ``None``. A plain (not async) fixture so
    the database is built outside the event loop. Module-level detection means a new such test is
    covered without being edited; one that never touches the engine builds no scratch database."""
    if not ({"get_engine", "get_sessionmaker"} & vars(request.module).keys()):
        return None
    return request.getfixturevalue("committed_database_url")  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
async def _process_wide_engine_on_scratch(_scratch_url_for_engine_modules, monkeypatch):
    """Point the process-wide engine at the scratch database for those modules - the tests of the
    worker, the stale-job recoveries and the services that really commit. They wrote audit rows to
    the development database too (one ``script.generation_enqueued`` per full run, found when the
    suite was run for Piece 3)."""
    url = _scratch_url_for_engine_modules
    if url is None:
        yield
        return
    import edutoon.db.database as database
    import edutoon.db.session as session_module

    real_new_engine = database.new_engine
    await database.dispose_engine()
    session_module._sessionmaker = None
    # A test may dispose the engine and ask for a new one: that one must be the scratch one too.
    monkeypatch.setattr(
        database, "new_engine", lambda explicit_url=None: real_new_engine(explicit_url or url)
    )
    yield
    await database.dispose_engine()
    session_module._sessionmaker = None


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
