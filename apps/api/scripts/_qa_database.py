"""A database of its own for UI QA. DEVELOPMENT ONLY.

``dev_qa_api.py`` and ``dev_qa_worker.py`` sign in as a stand-in person. If they used the normal
development database, that person's test projects would sit beside real ones and show up in the
normal app. Both launchers therefore call :func:`use_qa_database` first: it points this process at
``edutoon_qa`` (created and migrated on first use), leaving the normal database untouched.
There is deliberately no switch back to the normal database.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

QA_DATABASE_NAME = "edutoon_qa"
API_ROOT = Path(__file__).resolve().parents[1]


def _with_name(url: str, name: str) -> str:
    return make_url(url).set(database=name).render_as_string(hide_password=False)


async def _ensure_exists(admin_url: str) -> bool:
    """Create the QA database if missing. Returns ``True`` when it was just created."""
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            found = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": QA_DATABASE_NAME}
            )
            if found:
                return False
            await connection.execute(text(f'CREATE DATABASE "{QA_DATABASE_NAME}"'))
            return True
    finally:
        await engine.dispose()


def use_qa_database() -> str:
    """Point this process at the QA database (creating it first), and migrate it to head."""
    from edutoon.core.config import get_settings

    settings = get_settings()
    direct = settings.DATABASE_DIRECT_URL
    if make_url(direct).database == QA_DATABASE_NAME:
        raise SystemExit("The QA database is already the configured database; refusing to loop.")
    asyncio.run(_ensure_exists(_with_name(direct, "postgres")))
    qa_direct = _with_name(direct, QA_DATABASE_NAME)
    qa_pooled = _with_name(settings.DATABASE_URL, QA_DATABASE_NAME)
    os.environ["DATABASE_URL"] = qa_pooled
    os.environ["DATABASE_DIRECT_URL"] = qa_direct
    get_settings.cache_clear()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_ROOT,
        env={**os.environ},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"Migrating the QA database failed:\n{result.stdout}{result.stderr}")
    print(f"UI QA is using its own database '{QA_DATABASE_NAME}', not the normal one.")
    return qa_direct
