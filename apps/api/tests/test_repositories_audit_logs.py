from __future__ import annotations

from uuid import uuid4

from edutoon.repositories import audit_logs as audit_logs_repo


async def test_create_defaults(db_session):
    entity_id = uuid4()

    log = await audit_logs_repo.create(
        db_session, action="project.created", entity_type="project", entity_id=entity_id
    )

    assert log.actor_type == "user"
    assert log.context == {}


async def test_list_by_entity(db_session):
    entity_id = uuid4()
    other_entity_id = uuid4()
    log = await audit_logs_repo.create(
        db_session, action="project.created", entity_type="project", entity_id=entity_id
    )
    await audit_logs_repo.create(
        db_session, action="project.created", entity_type="project", entity_id=other_entity_id
    )

    page = await audit_logs_repo.list_by_entity(
        db_session, entity_type="project", entity_id=entity_id
    )

    assert [item.id for item in page.items] == [log.id]


async def test_list_by_project(db_session):
    project_id = uuid4()
    log = await audit_logs_repo.create(
        db_session, action="project.created", entity_type="project", project_id=project_id
    )

    page = await audit_logs_repo.list_by_project(db_session, project_id)

    assert [item.id for item in page.items] == [log.id]
