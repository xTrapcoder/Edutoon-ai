"""Alembic migration environment (async).

Migrations connect through ``DATABASE_DIRECT_URL`` — a direct, un-pooled
connection — rather than ``DATABASE_URL``, which in deployed environments may
point at a transaction-mode pooler (PgBouncer) that cannot run DDL reliably.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from edutoon.db.base import Base

# Import model modules here as they are introduced so that a future
# `alembic revision --autogenerate` can see them. None exist in Phase 2.

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the migration connection URL.

    Prefer explicit environment variables so migration tooling keeps working
    even when unrelated application settings are absent; fall back to the
    validated application settings otherwise.
    """
    url = os.environ.get("DATABASE_DIRECT_URL") or os.environ.get("DATABASE_URL")
    if url:
        return url

    from edutoon.core.config import get_settings

    return get_settings().DATABASE_DIRECT_URL


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
