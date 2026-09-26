"""Migration 0010 upgraded and downgraded for real, with Alembic, on a scratch database.

``test_migration_0010_schema.py`` checks the finished schema on the shared test
database. What it cannot check is the *journey*: that a database holding projects
written before 0010 upgrades cleanly and those projects come out working, and that
``downgrade`` really takes the change back out. Rolling the shared database back
mid-suite would pull the schema out from under every other test, so each test here
creates its own database, runs the real ``alembic`` command line against it (as
``make migrate`` / ``make migrate-down`` do), and drops it afterwards.

These are plain (sync) tests: ``alembic/env.py`` starts its own event loop, which it
cannot do inside a running one.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from edutoon.core.config import get_settings

API_ROOT = Path(__file__).resolve().parents[1]

_NEW_COLUMNS = {"orientation", "target_duration_ms"}
_NEW_CONSTRAINTS = {
    "ck_projects_orientation",
    "ck_projects_target_duration_ms_range",
    "ck_projects_language",
}


async def _admin(base: URL, statement: str) -> None:
    engine = create_async_engine(base, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture
def scratch_url() -> Iterator[str]:
    """An empty database of its own, dropped again whatever the test does."""
    base = make_url(get_settings().DATABASE_DIRECT_URL)
    name = f"edutoon_migration_test_{uuid4().hex[:12]}"
    asyncio.run(_admin(base, f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(_admin(base, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _alembic(url: str, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env={**os.environ, "DATABASE_DIRECT_URL": url},
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


def _sql(url: str, statement: str, **params: Any) -> list[dict[str, Any]]:
    return asyncio.run(_sql_async(url, statement, params))


def _projects_columns(url: str) -> set[str]:
    rows = _sql(
        url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'projects'",
    )
    return {row["column_name"] for row in rows}


def _projects_constraints(url: str) -> set[str]:
    rows = _sql(
        url,
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'public.projects'::regclass AND contype = 'c'",
    )
    return {row["conname"] for row in rows}


def _add_owner(url: str) -> Any:
    email = f"{uuid4()}@example.com"
    [row] = _sql(url, "INSERT INTO users (email) VALUES (:email) RETURNING id", email=email)
    return row["id"]


def _add_project(url: str, owner_id: Any, title: str, **columns: Any) -> None:
    names = ["owner_id", "title", "source_type", *columns]
    _sql(
        url,
        f"INSERT INTO projects ({', '.join(names)}) "
        f"VALUES ({', '.join(f':{name}' for name in names)})",
        owner_id=owner_id,
        title=title,
        source_type="topic",
        **columns,
    )


def _project(url: str, title: str) -> dict[str, Any]:
    [row] = _sql(url, "SELECT * FROM projects WHERE title = :title", title=title)
    return row


def test_upgrade_gives_existing_projects_the_defaults_and_keeps_them_working(scratch_url):
    _alembic(scratch_url, "upgrade", "0009")
    assert not _NEW_COLUMNS & _projects_columns(scratch_url)
    assert not _NEW_CONSTRAINTS & _projects_constraints(scratch_url)
    owner = _add_owner(scratch_url)
    _add_project(scratch_url, owner, "legacy-default")  # language takes 0002's default
    _add_project(scratch_url, owner, "legacy-en-in", language="en-IN")

    _alembic(scratch_url, "upgrade", "0010")

    assert _projects_columns(scratch_url) >= _NEW_COLUMNS
    assert _projects_constraints(scratch_url) >= _NEW_CONSTRAINTS
    for title, language in [("legacy-default", "en-GB"), ("legacy-en-in", "en-IN")]:
        project = _project(scratch_url, title)
        assert project["orientation"] == "horizontal"
        assert project["target_duration_ms"] == 300_000
        assert project["language"] == language  # a supported language is never touched
    assert len(_sql(scratch_url, "SELECT id FROM projects")) == 2
    # And the upgraded database enforces exactly what the new code relies on.
    _add_project(scratch_url, owner, "minimum", target_duration_ms=60_000)
    _add_project(scratch_url, owner, "maximum", target_duration_ms=3_600_000)
    _add_project(scratch_url, owner, "vertical", orientation="vertical")
    _add_project(scratch_url, owner, "en-in", language="en-IN")
    for columns, constraint in [
        ({"target_duration_ms": 59_999}, "ck_projects_target_duration_ms_range"),
        ({"target_duration_ms": 3_600_001}, "ck_projects_target_duration_ms_range"),
        ({"orientation": "square"}, "ck_projects_orientation"),
        ({"language": "fr-FR"}, "ck_projects_language"),
    ]:
        with pytest.raises(IntegrityError, match=constraint):
            _add_project(scratch_url, owner, "rejected", **columns)


def test_upgrade_resets_a_language_the_old_schema_accepted_but_the_new_one_does_not(scratch_url):
    """``projects.language`` was unconstrained free text before 0010. A row holding
    something outside the supported set is reset to the default so the constraint can
    be added at all; the upgrade must not fail on it.
    """
    _alembic(scratch_url, "upgrade", "0009")
    owner = _add_owner(scratch_url)
    _add_project(scratch_url, owner, "french", language="fr-FR")
    _add_project(scratch_url, owner, "lowercase", language="en-in")
    _add_project(scratch_url, owner, "blank", language="")

    _alembic(scratch_url, "upgrade", "0010")

    for title in ("french", "lowercase", "blank"):
        assert _project(scratch_url, title)["language"] == "en-GB"


def test_alembic_reports_0010_as_the_single_head_after_upgrading(scratch_url):
    _alembic(scratch_url, "upgrade", "head")

    assert "0010" in _alembic(scratch_url, "current")
    heads = _alembic(scratch_url, "heads").split()
    assert heads.count("(head)") == 1


def test_downgrade_removes_the_columns_and_constraints_and_keeps_the_projects(scratch_url):
    _alembic(scratch_url, "upgrade", "0010")
    owner = _add_owner(scratch_url)
    _add_project(
        scratch_url,
        owner,
        "kept",
        language="en-IN",
        orientation="vertical",
        target_duration_ms=1_800_000,
    )

    _alembic(scratch_url, "downgrade", "0009")

    assert not _NEW_COLUMNS & _projects_columns(scratch_url)
    assert not _NEW_CONSTRAINTS & _projects_constraints(scratch_url)
    kept = _project(scratch_url, "kept")
    assert kept["language"] == "en-IN"
    assert "orientation" not in kept
    # The old schema's free-text language is free text again.
    _sql(scratch_url, "UPDATE projects SET language = 'fr-FR' WHERE title = 'kept'")


def test_the_migration_round_trips(scratch_url):
    """upgrade -> downgrade -> upgrade lands on the same schema, and the second upgrade
    copes with data the first downgrade let in.
    """
    _alembic(scratch_url, "upgrade", "0010")
    columns = _projects_columns(scratch_url)
    constraints = _projects_constraints(scratch_url)
    owner = _add_owner(scratch_url)

    _alembic(scratch_url, "downgrade", "0009")
    _add_project(scratch_url, owner, "written-while-downgraded", language="de-DE")
    _alembic(scratch_url, "upgrade", "0010")

    assert _projects_columns(scratch_url) == columns
    assert _projects_constraints(scratch_url) == constraints
    project = _project(scratch_url, "written-while-downgraded")
    assert project["language"] == "en-GB"
    assert (project["orientation"], project["target_duration_ms"]) == ("horizontal", 300_000)
