"""Regression coverage for the bug where `get_session` never called
`session.commit()`, so every write made through the production dependency
was silently rolled back the instant the request ended - nothing ever
persisted. Every other test in this suite uses the `db_session` fixture,
which binds directly to a connection and deliberately never commits, so it
cannot catch this class of bug. This file exercises the real dependency
instead.
"""

from __future__ import annotations

import contextlib
from uuid import uuid4

from sqlalchemy import select

import edutoon.db.session as session_module
from edutoon.db.database import dispose_engine, get_engine
from edutoon.db.session import get_session
from edutoon.db.tables import users
from edutoon.repositories import users as users_repo


async def test_get_session_commits_writes_that_survive_the_dependency_completing():
    email = f"{uuid4()}@example.com"
    engine = get_engine()

    try:
        session_gen = get_session()
        session = await anext(session_gen)
        created = await users_repo.create(session, email=email)

        # Drive the dependency generator to completion exactly as FastAPI
        # does once a request finishes successfully - this is what runs the
        # `await session.commit()` on the success path.
        with contextlib.suppress(StopAsyncIteration):
            await anext(session_gen)

        # A brand new connection, entirely independent of the session above,
        # must see the row. If `get_session` still didn't commit, this comes
        # back empty even though `create` above returned successfully.
        async with engine.connect() as verification_conn:
            row = (
                await verification_conn.execute(
                    select(users.c.id).where(users.c.email == email)
                )
            ).first()

        assert row is not None
        assert row.id == created.id
    finally:
        async with engine.begin() as cleanup_conn:
            await cleanup_conn.execute(users.delete().where(users.c.email == email))
        await dispose_engine()
        # `get_sessionmaker()` caches a sessionmaker bound to whichever engine
        # was current when it was first called; `dispose_engine()` only
        # resets the engine singleton, so this must be cleared too or a
        # later test would get a sessionmaker bound to a disposed engine.
        session_module._sessionmaker = None
