from __future__ import annotations

from uuid import uuid4

from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo


async def _make_project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="topic"
    )


async def test_create_defaults_to_queued(db_session):
    project = await _make_project(db_session)

    job = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")

    assert job.status == "queued"
    assert job.attempts == 0
    assert job.payload == {}


async def test_get_by_idempotency_key(db_session):
    project = await _make_project(db_session)
    job = await jobs_repo.create(
        db_session,
        project_id=project.id,
        kind="extract_topics",
        idempotency_key="key-1",
    )

    found = await jobs_repo.get_by_idempotency_key(
        db_session, project_id=project.id, kind="extract_topics", idempotency_key="key-1"
    )

    assert found == job
    assert (
        await jobs_repo.get_by_idempotency_key(
            db_session, project_id=project.id, kind="extract_topics", idempotency_key="missing"
        )
        is None
    )


async def test_list_by_project(db_session):
    project = await _make_project(db_session)
    other_project = await _make_project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    await jobs_repo.create(db_session, project_id=other_project.id, kind="extract_topics")

    page = await jobs_repo.list_by_project(db_session, project.id)

    assert [item.id for item in page.items] == [job.id]


async def test_update_applies_a_status_transition(db_session):
    project = await _make_project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")

    updated = await jobs_repo.update(
        db_session, job_id=job.id, status="succeeded", result={"chunks_embedded": 3}
    )

    assert updated is not None
    assert updated.status == "succeeded"
    assert updated.result == {"chunks_embedded": 3}


async def test_update_with_no_fields_is_a_read(db_session):
    project = await _make_project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")

    assert await jobs_repo.update(db_session, job_id=job.id) == job


async def test_update_returns_none_when_missing(db_session):
    assert await jobs_repo.update(db_session, job_id=uuid4(), status="failed") is None
