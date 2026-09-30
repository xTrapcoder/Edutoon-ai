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

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from edutoon.core.config import get_settings  # noqa: E402
from edutoon.providers.cache import Redis, get_redis_client  # noqa: E402


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
