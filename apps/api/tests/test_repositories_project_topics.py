from __future__ import annotations

from uuid import uuid4

from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo


async def _make_project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="topic"
    )


async def test_create_and_get_by_id(db_session):
    project = await _make_project(db_session)

    topic = await project_topics_repo.create(db_session, project_id=project.id, title="Intro")

    assert topic.status == "proposed"
    assert topic.parent_id is None
    assert await project_topics_repo.get_by_id(db_session, topic.id) == topic


async def test_child_topic_references_parent(db_session):
    project = await _make_project(db_session)
    parent = await project_topics_repo.create(db_session, project_id=project.id, title="Root")

    child = await project_topics_repo.create(
        db_session, project_id=project.id, title="Child", parent_id=parent.id, depth=1
    )

    assert child.parent_id == parent.id


async def test_list_by_project(db_session):
    project = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project.id, title="Intro")

    page = await project_topics_repo.list_by_project(db_session, project.id)

    assert [item.id for item in page.items] == [topic.id]
