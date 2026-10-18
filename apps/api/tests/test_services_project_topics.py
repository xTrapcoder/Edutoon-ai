"""``services/project_topics.py`` - the keyword and review operations behind the
topics API (Phase 5, Step 6).

The HTTP contract is covered in ``test_topics_api.py``; this file is about the
rules the *service* owns and must not delegate to the schema layer, and about
the two things only a real second connection can prove: concurrent keyword
additions do not collide on a position, and no topic edit can slip in beside an
extraction that is being enqueued.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

import edutoon.db.session as session_module
from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, NotFoundError, ValidationError
from edutoon.db.database import dispose_engine, get_engine
from edutoon.db.session import get_sessionmaker
from edutoon.db.tables import users as users_table
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo
from edutoon.services import project_topics as service
from edutoon.services import topic_extraction as topic_extraction_service


async def _project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Photosynthesis", source_type="pdf"
    )
    return owner, project


async def _add(db_session, owner, project, *keywords: str):
    return await service.add_keyword_topics(
        db_session, project_id=project.id, owner_id=owner.id, keywords=list(keywords)
    )


async def _patch(db_session, owner, project, topic_id: UUID, **fields: Any):
    return await service.update_topic(
        db_session, project_id=project.id, owner_id=owner.id, topic_id=topic_id, fields=fields
    )


async def _stored(db_session, project):
    return [
        (t.title, t.status, t.summary, t.position)
        for t in await project_topics_repo.list_all_by_project(db_session, project.id)
    ]


# --- add_keyword_topics ------------------------------------------------------------------


async def test_keywords_count_towards_the_cap_even_when_rejected(db_session):
    """A rejected topic can be accepted again, so it still occupies a place: were
    it not counted, un-rejecting could push the project past what one extraction
    can carry.
    """
    limit = get_settings().MAX_TOPICS_PER_EXTRACTION
    owner, project = await _project(db_session)
    created = await _add(db_session, owner, project, *[f"Keyword {n}" for n in range(limit)])
    await _patch(db_session, owner, project, created[0].topic.id, status="rejected")

    with pytest.raises(ValidationError, match="at most"):
        await _add(db_session, owner, project, "One too many")

    assert len(await _stored(db_session, project)) == limit


async def test_keywords_only_count_the_users_own_topics_towards_the_cap(db_session):
    limit = get_settings().MAX_TOPICS_PER_EXTRACTION
    owner, project = await _project(db_session)
    for n in range(limit):
        await project_topics_repo.create(
            db_session, project_id=project.id, title=f"Derived {n}", position=n, origin="suggested"
        )

    added = await _add(db_session, owner, project, "Mine")

    assert [a.topic.title for a in added] == ["Mine"]


async def test_a_duplicate_is_detected_against_rejected_and_differently_spaced_topics(db_session):
    owner, project = await _project(db_session)
    [first] = await _add(db_session, owner, project, "The Calvin cycle")
    await _patch(db_session, owner, project, first.topic.id, status="rejected")

    with pytest.raises(ConflictError, match="already has a topic"):
        await _add(db_session, owner, project, "the   calvin CYCLE")


async def test_a_derived_topic_with_the_same_title_does_not_block_a_keyword(db_session):
    owner, project = await _project(db_session)
    await project_topics_repo.create(
        db_session, project_id=project.id, title="Chlorophyll", position=0, origin="source"
    )

    added = await _add(db_session, owner, project, "Chlorophyll")

    assert added[0].topic.origin == "user"


async def test_keywords_for_a_project_the_caller_does_not_own_are_a_404(db_session):
    _, project = await _project(db_session)
    stranger = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await _add(db_session, stranger, project, "Photosynthesis")

    assert await _stored(db_session, project) == []


# --- update_topic --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "archived"},
        {"status": None},
        {"status": 1},
        {"title": None},
        {"title": 5},
        {"summary": 5},
        {"origin": "source"},
        {"position": 4},
        {"parent_id": None},
    ],
)
async def test_update_rejects_what_the_schema_would_also_reject(db_session, fields):
    """The service enforces its own invariants: none of these may reach the
    database (where they would surface as a NOT NULL / CHECK violation, i.e. a
    misleading "conflict") or change an immutable column.
    """
    owner, project = await _project(db_session)
    [topic] = await _add(db_session, owner, project, "Photosynthesis")
    before = await _stored(db_session, project)

    with pytest.raises(ValidationError):
        await _patch(db_session, owner, project, topic.topic.id, **fields)

    assert await _stored(db_session, project) == before


async def test_a_partial_failure_changes_nothing(db_session):
    """A valid status alongside an invalid title: the whole update is refused."""
    owner, project = await _project(db_session)
    [topic] = await _add(db_session, owner, project, "Photosynthesis")

    with pytest.raises(ValidationError):
        await _patch(db_session, owner, project, topic.topic.id, status="rejected", title="  ")

    assert (await _stored(db_session, project))[0][1] == "accepted"


async def test_update_reports_a_topic_from_another_project_as_missing(db_session):
    owner, home = await _project(db_session)
    away = await projects_repo.create(
        db_session, owner_id=owner.id, title="Elsewhere", source_type="pdf"
    )
    [topic] = await _add(db_session, owner, home, "Photosynthesis")

    with pytest.raises(NotFoundError):
        await _patch(db_session, owner, away, topic.topic.id, status="rejected")

    assert (await _stored(db_session, home))[0][1] == "accepted"


async def test_a_derived_topic_may_take_a_title_another_derived_topic_has(db_session):
    owner, project = await _project(db_session)
    await project_topics_repo.create(
        db_session, project_id=project.id, title="Overview", position=0, origin="source"
    )
    other = await project_topics_repo.create(
        db_session, project_id=project.id, title="Details", position=1, origin="source"
    )

    updated = await _patch(db_session, owner, project, other.id, title="Overview")

    assert updated.topic.title == "Overview"


# --- concurrency: real second connection ---------------------------------------------------
#
# These commit for real, so they clean up by hand and keep the append-only audit
# table out of it (its rows cannot be deleted; nothing under test depends on it) -
# the same approach as the enqueue race in test_services_topic_extraction.py.


async def _committed_project(sessionmaker):
    async with sessionmaker() as setup:
        owner = await users_repo.create(setup, email=f"{uuid4()}@example.com")
        project = await projects_repo.create(
            setup, owner_id=owner.id, title="Race", source_type="pdf"
        )
        await setup.commit()
    return owner, project


async def _give_material(sessionmaker, project) -> None:
    """One parsed page, so a topic extraction can be enqueued (keywords alone are never enough)."""
    from tests.test_services_topic_extraction import _add_chunks

    async with sessionmaker() as setup:
        await _add_chunks(setup, project, ["Plants make food from light."])
        await setup.commit()


async def _cleanup(owner) -> None:
    async with get_engine().begin() as cleanup:
        await cleanup.execute(users_table.delete().where(users_table.c.id == owner.id))
    await dispose_engine()
    session_module._sessionmaker = None


async def test_two_simultaneous_keyword_additions_both_succeed_in_order(monkeypatch):
    """Both compute "the next free position" from the same snapshot; the lock is
    what makes the second wait and see the first's rows, instead of colliding on
    the unique position index.
    """
    monkeypatch.setattr(service.audit_logs_service, "record_audit_log", AsyncMock())
    sessionmaker = get_sessionmaker()
    owner, project = await _committed_project(sessionmaker)

    try:
        async with sessionmaker() as first, sessionmaker() as second:
            await service.add_keyword_topics(
                first, project_id=project.id, owner_id=owner.id, keywords=["Alpha"]
            )  # holds the project's lock, uncommitted
            waiting = asyncio.create_task(
                service.add_keyword_topics(
                    second, project_id=project.id, owner_id=owner.id, keywords=["Beta"]
                )
            )
            await asyncio.sleep(0.6)
            assert not waiting.done(), "the second addition did not wait for the first"

            await first.commit()
            await asyncio.wait_for(waiting, timeout=10)
            await second.commit()

        async with sessionmaker() as check:
            stored = await project_topics_repo.list_all_by_project(check, project.id)
        assert [(t.title, t.position) for t in stored] == [("Alpha", 0), ("Beta", 1)]
    finally:
        await _cleanup(owner)


async def test_keywords_cannot_be_added_beside_an_extraction_being_enqueued(monkeypatch):
    """The extraction is enqueued (uncommitted, holding the lock). A keyword added
    now would miss that run and could take a position the run is about to use, so
    it must wait, then be refused - and write nothing.
    """
    monkeypatch.setattr(service.audit_logs_service, "record_audit_log", AsyncMock())
    monkeypatch.setattr(
        topic_extraction_service.audit_logs_service, "record_audit_log", AsyncMock()
    )
    sessionmaker = get_sessionmaker()
    owner, project = await _committed_project(sessionmaker)
    await _give_material(sessionmaker, project)
    async with sessionmaker() as seed:
        await project_topics_repo.create(seed, project_id=project.id, title="Alpha", position=0)
        await seed.commit()

    try:
        async with sessionmaker() as first, sessionmaker() as second:
            enqueued = await topic_extraction_service.enqueue_topic_extraction(
                first, project_id=project.id, owner_id=owner.id, idempotency_key="k"
            )
            waiting = asyncio.create_task(
                service.add_keyword_topics(
                    second, project_id=project.id, owner_id=owner.id, keywords=["Beta"]
                )
            )
            await asyncio.sleep(0.6)
            assert not waiting.done(), "the keyword addition did not wait for the enqueue"

            await first.commit()
            with pytest.raises(ConflictError, match="already queued or running"):
                await asyncio.wait_for(waiting, timeout=10)
            await second.rollback()

        async with sessionmaker() as check:
            stored = await project_topics_repo.list_all_by_project(check, project.id)
            active = await jobs_repo.get_active_by_project_kind(
                check, project_id=project.id, kind="extract_topics"
            )
        assert [t.title for t in stored] == ["Alpha"]
        assert active is not None and active.id == enqueued.job.id
    finally:
        await _cleanup(owner)


async def test_a_wording_edit_cannot_slip_in_beside_an_extraction_being_enqueued(monkeypatch):
    monkeypatch.setattr(service.audit_logs_service, "record_audit_log", AsyncMock())
    monkeypatch.setattr(
        topic_extraction_service.audit_logs_service, "record_audit_log", AsyncMock()
    )
    sessionmaker = get_sessionmaker()
    owner, project = await _committed_project(sessionmaker)
    await _give_material(sessionmaker, project)
    async with sessionmaker() as seed:
        topic = await project_topics_repo.create(
            seed, project_id=project.id, title="Alpha", position=0
        )
        await seed.commit()

    try:
        async with sessionmaker() as first, sessionmaker() as second:
            await topic_extraction_service.enqueue_topic_extraction(
                first, project_id=project.id, owner_id=owner.id, idempotency_key="k"
            )
            waiting = asyncio.create_task(
                service.update_topic(
                    second,
                    project_id=project.id,
                    owner_id=owner.id,
                    topic_id=topic.id,
                    fields={"summary": "Mine"},
                )
            )
            await asyncio.sleep(0.6)
            assert not waiting.done(), "the edit did not wait for the enqueue"

            await first.commit()
            with pytest.raises(ConflictError, match="already queued or running"):
                await asyncio.wait_for(waiting, timeout=10)
            await second.rollback()

        async with sessionmaker() as check:
            stored = await project_topics_repo.get_by_id(check, topic.id)
        assert stored is not None and stored.summary is None
    finally:
        await _cleanup(owner)


async def test_a_no_op_update_cannot_read_a_topic_from_another_project(db_session):
    owner, home = await _project(db_session)
    away = await projects_repo.create(
        db_session, owner_id=owner.id, title="Elsewhere", source_type="pdf"
    )
    [topic] = await _add(db_session, owner, home, "Photosynthesis")

    with pytest.raises(NotFoundError):
        await _patch(db_session, owner, away, topic.topic.id, status="accepted")
