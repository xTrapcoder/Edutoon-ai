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
