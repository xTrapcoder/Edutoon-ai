from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.core.errors import NotFoundError
from edutoon.services import projects as projects_service
from edutoon.services import users as users_service


async def test_get_user_or_404_raises_when_missing(db_session):
    with pytest.raises(NotFoundError):
        await users_service.get_user_or_404(db_session, uuid4())


async def test_get_user_or_404_returns_the_user_when_present(db_session):
    created = await users_service.create_user(db_session, email=f"{uuid4()}@example.com")

    fetched = await users_service.get_user_or_404(db_session, created.id)

    assert fetched == created


async def test_get_project_or_404_raises_when_missing(db_session):
    with pytest.raises(NotFoundError):
        await projects_service.get_project_or_404(db_session, uuid4())
