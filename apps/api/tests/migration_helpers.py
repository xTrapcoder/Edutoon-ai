"""What the tests that run Alembic against a scratch database share.

Rolling the shared test database up and down mid-suite would pull the schema out
from under every other test, so a migration's journey is tested on a database of its
own: :func:`scratch_database` creates one, :func:`run_alembic` runs the real
``alembic`` command line against it (as ``make migrate`` / ``make migrate-down`` do),
and the other helpers write and read it with plain SQL.

Everything here is synchronous: ``alembic/env.py`` starts its own event loop, which it
cannot do inside a running one, so the tests that use these are plain ``def`` tests.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from edutoon.core.config import get_settings

API_ROOT = Path(__file__).resolve().parents[1]


async def _admin(base: URL, statement: str) -> None:
    engine = create_async_engine(base, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@contextmanager
def scratch_database() -> Iterator[str]:
    """An empty database of its own (yields its URL), dropped again whatever happens."""
    base = make_url(get_settings().DATABASE_DIRECT_URL)
    name = f"edutoon_migration_test_{uuid4().hex[:12]}"
    asyncio.run(_admin(base, f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(_admin(base, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def run_alembic(url: str, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env={**os.environ, "DATABASE_DIRECT_URL": url, **(env or {})},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    command = " ".join(args)
    assert result.returncode == 0, f"alembic {command} failed:\n{result.stdout}{result.stderr}"
    return result.stdout + result.stderr


async def _sql_async(url: str, statement: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return [dict(row._mapping) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


def sql(url: str, statement: str, **params: Any) -> list[dict[str, Any]]:
    return asyncio.run(_sql_async(url, statement, params))


def projects_columns(url: str) -> set[str]:
    rows = sql(
        url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'projects'",
    )
    return {row["column_name"] for row in rows}


def projects_constraints(url: str) -> set[str]:
    rows = sql(
        url,
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'public.projects'::regclass AND contype = 'c'",
    )
    return {row["conname"] for row in rows}


def add_owner(url: str) -> Any:
    email = f"{uuid4()}@example.com"
    # Migration 0019 added ``users.role`` (NOT NULL, no default); older revisions lack it.
    has_role = sql(
        url,
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'role'",
    )
    columns, values = ("email, role", ":email, 'admin'") if has_role else ("email", ":email")
    [row] = sql(url, f"INSERT INTO users ({columns}) VALUES ({values}) RETURNING id", email=email)
    return row["id"]


def add_project(url: str, owner_id: Any, title: str, **columns: Any) -> None:
    """INSERT a project naming only ``columns`` beyond the required ones."""
    names = ["owner_id", "title", "source_type", *columns]
    sql(
        url,
        f"INSERT INTO projects ({', '.join(names)}) "
        f"VALUES ({', '.join(f':{name}' for name in names)})",
        owner_id=owner_id,
        title=title,
        source_type="topic",
        **columns,
    )


def project_row(url: str, title: str) -> dict[str, Any]:
    [row] = sql(url, "SELECT * FROM projects WHERE title = :title", title=title)
    return row
