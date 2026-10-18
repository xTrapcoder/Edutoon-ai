"""Stale-extraction recovery (Phase 5 hardening).

A worker that crashes mid-extraction leaves its job ``running`` for ever, which
used to block the project's next extraction, keyword additions and title/summary
edits with no way out. Every one of those goes through
``topic_extraction.refuse_if_extraction_active``, which now fails an abandoned
job first.

The property that matters most is the other half: **a job that is genuinely
being worked on is never failed**, however long it has been running. A live
worker's handler holds its job's row lock for the whole run, so "stale" means
*old and unlocked*. Row locks only exist between connections, so the tests that
prove this use two real ones (and clean up by hand, keeping the append-only audit
table out of it - the same approach as the enqueue race in
``test_services_topic_extraction.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

import edutoon.db.session as session_module
from edutoon.core.config import Settings, get_settings
from edutoon.core.errors import ConflictError
from edutoon.db.database import dispose_engine, get_engine
from edutoon.db.session import get_sessionmaker
from edutoon.db.tables import jobs as jobs_table
from edutoon.db.tables import users as users_table
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import project_topics as topics_service
from edutoon.services import topic_extraction as service

KIND = "extract_topics"


async def _project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Photosynthesis", source_type="pdf"
    )
    return owner, project


async def _job(
    session,
    project,
    *,
    status: str = "running",
    started_ago: timedelta | None = timedelta(hours=2),
    kind: str = KIND,
):
    """A job in ``status``. A running job started ``started_ago`` ago (or never,
    with ``None``); a finished one also has a ``finished_at``.
    """
    job = await jobs_repo.create(session, project_id=project.id, kind=kind)
    fields: dict[str, Any] = {"status": status}
    if started_ago is not None:
        fields["started_at"] = datetime.now(UTC) - started_ago
    if status in ("succeeded", "failed"):
        fields["finished_at"] = datetime.now(UTC)
    updated = await jobs_repo.update(session, job_id=job.id, **fields)
    assert updated is not None
    return updated


async def _status(session, job_id) -> str:
    job = await jobs_repo.get_by_id(session, job_id)
    assert job is not None
    return job.status


def _with_threshold(monkeypatch: Any, seconds: int) -> None:
    settings = get_settings().model_copy(update={"TOPIC_EXTRACTION_STALE_AFTER_SECONDS": seconds})
    monkeypatch.setattr(service, "get_settings", lambda: settings)


async def _audit(session, project) -> list[Any]:
    page = await audit_logs_service.list_audit_logs_for_project(session, project.id)
    return [e for e in page.items if e.action == "topics.extraction_recovered"]


# --- recovery: an abandoned job stops blocking ------------------------------------------


async def test_an_abandoned_extraction_no_longer_blocks_adding_keywords(db_session):
    owner, project = await _project(db_session)
    job = await _job(db_session, project)

    added = await topics_service.add_keyword_topics(
        db_session, project_id=project.id, owner_id=owner.id, keywords=["Photosynthesis"]
    )

    assert [a.topic.title for a in added] == ["Photosynthesis"]
    assert await _status(db_session, job.id) == "failed"


async def test_an_abandoned_extraction_no_longer_blocks_a_new_extraction(db_session):
    owner, project = await _project(db_session)
    await project_topics_repo.create(db_session, project_id=project.id, title="Cells")
    from tests.test_services_topic_extraction import _add_chunks

    await _add_chunks(db_session, project, ["Cells are the unit of life."])
    stale = await _job(db_session, project)

    fresh = await service.enqueue_topic_extraction(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="again"
    )

    assert fresh.replayed is False and fresh.job.id != stale.id
    assert fresh.job.status == "queued"
    assert await _status(db_session, stale.id) == "failed"


async def test_an_abandoned_extraction_no_longer_blocks_a_wording_edit(db_session):
    owner, project = await _project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project.id, title="Cells")
    await _job(db_session, project)

    updated = await topics_service.update_topic(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        topic_id=topic.id,
        fields={"summary": "The unit of life."},
    )

    assert updated.topic.summary == "The unit of life."


async def test_a_recovered_job_is_a_normal_failed_job_and_says_what_happened(db_session):
    owner, project = await _project(db_session)
    stale = await _job(db_session, project)

    await service.refuse_if_extraction_active(db_session, project_id=project.id)

    after = await jobs_repo.get_by_id(db_session, stale.id)
    assert after is not None
    assert after.status == "failed"
    assert after.error == service.STALE_EXTRACTION_ERROR
    assert "run the extraction again" in (after.error or "")
    assert after.finished_at is not None and after.started_at == stale.started_at
    assert after.attempts == stale.attempts  # recovery is not an attempt
    assert after.result is None


async def test_recovery_is_audited_once_as_a_system_action(db_session):
    _, project = await _project(db_session)
    stale = await _job(db_session, project)

    await service.refuse_if_extraction_active(db_session, project_id=project.id)
    await service.refuse_if_extraction_active(db_session, project_id=project.id)  # nothing left

    [entry] = await _audit(db_session, project)
    assert entry.actor_type == "system" and entry.actor_id is None
    assert entry.entity_id == stale.id
    assert entry.context["job_id"] == str(stale.id)
    assert (
        entry.context["stale_after_seconds"] == get_settings().TOPIC_EXTRACTION_STALE_AFTER_SECONDS
    )


async def test_a_running_job_with_no_started_at_is_recovered_from_its_creation_time(db_session):
    """Never expected, but a recovery that violated the finished-needs-started
    CHECK would fail the request instead of clearing the block.
    """
    _, project = await _project(db_session)
    job = await _job(db_session, project, started_ago=None)
    await db_session.execute(
        jobs_table.update()
        .where(jobs_table.c.id == job.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=2))
    )

    await service.refuse_if_extraction_active(db_session, project_id=project.id)

    after = await jobs_repo.get_by_id(db_session, job.id)
    assert after is not None and after.status == "failed"
    assert after.started_at is not None and after.finished_at is not None


# --- no premature recovery ----------------------------------------------------------------


async def test_a_recently_started_extraction_is_left_running_and_still_blocks(db_session):
    owner, project = await _project(db_session)
    running = await _job(db_session, project, started_ago=timedelta(minutes=5))

    with pytest.raises(ConflictError, match="already queued or running"):
        await topics_service.add_keyword_topics(
            db_session, project_id=project.id, owner_id=owner.id, keywords=["Photosynthesis"]
        )

    assert await _status(db_session, running.id) == "running"
    assert await _audit(db_session, project) == []


async def test_the_threshold_is_the_setting(db_session, monkeypatch):
    _with_threshold(monkeypatch, 60)
    owner, project = await _project(db_session)
    young = await _job(db_session, project, started_ago=timedelta(seconds=30))

    with pytest.raises(ConflictError):
        await service.refuse_if_extraction_active(db_session, project_id=project.id)
    assert await _status(db_session, young.id) == "running"

    old = await _job(db_session, project, started_ago=timedelta(seconds=120))
    # (the older one is recovered; the young one, still running, keeps blocking)
    with pytest.raises(ConflictError):
        await service.refuse_if_extraction_active(db_session, project_id=project.id)
    assert await _status(db_session, old.id) == "failed"
    assert await _status(db_session, young.id) == "running"


async def test_a_queued_job_is_never_treated_as_abandoned(db_session):
    """A queued job is waiting for a worker, not abandoned by one - however old."""
    owner, project = await _project(db_session)
    queued = await _job(db_session, project, status="queued", started_ago=None)
    await db_session.execute(
        jobs_table.update()
        .where(jobs_table.c.id == queued.id)
        .values(
            created_at=datetime.now(UTC) - timedelta(days=3),
            scheduled_at=datetime.now(UTC) - timedelta(days=3),
        )
    )

    with pytest.raises(ConflictError):
        await service.refuse_if_extraction_active(db_session, project_id=project.id)

    assert await _status(db_session, queued.id) == "queued"


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
async def test_finished_jobs_are_left_exactly_as_they_are(db_session, status):
    _, project = await _project(db_session)
    done = await _job(db_session, project, status=status, started_ago=timedelta(days=1))

    await service.refuse_if_extraction_active(db_session, project_id=project.id)

    after = await jobs_repo.get_by_id(db_session, done.id)
    assert after == done
    assert await _audit(db_session, project) == []


async def test_only_extraction_jobs_of_this_project_are_recovered(db_session):
    owner, project = await _project(db_session)
    other = await projects_repo.create(
        db_session, owner_id=owner.id, title="Elsewhere", source_type="pdf"
    )
    script_job = await _job(db_session, project, kind="generate_script")
    evidence_job = await _job(db_session, project, kind="build_evidence")
    elsewhere = await _job(db_session, other)

    await service.refuse_if_extraction_active(db_session, project_id=project.id)

    # Failing a script job would strand its still-`pending` script, so only the
    # extraction kind is ever recovered; and never across projects.
    assert await _status(db_session, script_job.id) == "running"
    assert await _status(db_session, evidence_job.id) == "running"
    assert await _status(db_session, elsewhere.id) == "running"


async def test_every_stale_extraction_of_the_project_is_recovered_together(db_session):
    _, project = await _project(db_session)
    first = await _job(db_session, project, started_ago=timedelta(hours=3))
    second = await _job(db_session, project, started_ago=timedelta(hours=2))

    await service.refuse_if_extraction_active(db_session, project_id=project.id)

    assert await _status(db_session, first.id) == "failed"
    assert await _status(db_session, second.id) == "failed"
    assert len(await _audit(db_session, project)) == 2


# --- the setting ---------------------------------------------------------------------------


def test_the_stale_timeout_is_optional_and_defaults_above_the_worst_model_call():
    default = Settings.model_fields["TOPIC_EXTRACTION_STALE_AFTER_SECONDS"].default
    # The SDK's 600 s read timeout over its three attempts is about 30 minutes.
    assert default >= 3 * 600 + 60


def test_a_timeout_short_enough_to_race_a_healthy_worker_is_rejected_at_start_up():
    with pytest.raises(PydanticValidationError):
        Settings(TOPIC_EXTRACTION_STALE_AFTER_SECONDS=59)


# --- live vs crashed worker: two real connections ------------------------------------------


async def _committed_running_job():
    """A project with a topic and an extraction that a worker claimed two hours
    ago (committed, as ``claim_next`` does), plus the cleanup for both.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as setup:
        owner = await users_repo.create(setup, email=f"{uuid4()}@example.com")
        project = await projects_repo.create(
            setup, owner_id=owner.id, title="Race", source_type="pdf"
        )
        await project_topics_repo.create(setup, project_id=project.id, title="Cells")
        job = await _job(setup, project)
        await setup.commit()
    return sessionmaker, owner, project, job


async def _cleanup(owner) -> None:
    async with get_engine().begin() as cleanup:
        await cleanup.execute(users_table.delete().where(users_table.c.id == owner.id))
    await dispose_engine()
    session_module._sessionmaker = None


def _quiet_audit(monkeypatch: Any) -> AsyncMock:
    """Keep the append-only audit table out of the tests that commit for real.
    Both services call the one shared ``audit_logs`` module, so this single patch
    covers them, and the mock records what they tried to write.
    """
    recorded = AsyncMock()
    monkeypatch.setattr(audit_logs_service, "record_audit_log", recorded)
    return recorded


async def test_a_job_a_live_worker_is_running_is_never_recovered_however_old(monkeypatch):
    """The worker's handler marks the job running inside the transaction it keeps
    open for the whole run, so the row stays locked. The job below is two hours
    old - past the threshold - yet must be left alone while that lock is held.
    """
    recorded = _quiet_audit(monkeypatch)
    sessionmaker, owner, project, job = await _committed_running_job()

    try:
        async with sessionmaker() as worker, sessionmaker() as api:
            # What run_extract_topics_job does first; the commit comes at the end.
            await jobs_repo.update(
                worker, job_id=job.id, status="running", started_at=datetime.now(UTC), attempts=1
            )

            with pytest.raises(ConflictError, match="already queued or running"):
                await topics_service.add_keyword_topics(
                    api, project_id=project.id, owner_id=owner.id, keywords=["Respiration"]
                )
            await api.rollback()

            async with sessionmaker() as observer:
                assert await _status(observer, job.id) == "running"
            recorded.assert_not_called()

            # The worker finishes normally...
            await jobs_repo.update(
                worker, job_id=job.id, status="succeeded", finished_at=datetime.now(UTC), result={}
            )
            await worker.commit()

            # ...and only then is the project free again.
            added = await topics_service.add_keyword_topics(
                api, project_id=project.id, owner_id=owner.id, keywords=["Respiration"]
            )
            await api.commit()
        assert [a.topic.title for a in added] == ["Respiration"]
    finally:
        await _cleanup(owner)


async def test_a_job_whose_worker_crashed_is_recovered(monkeypatch):
    """Same job, same age - but the worker's connection is gone (its transaction
    rolled back), so nothing holds the row any more.
    """
    recorded = _quiet_audit(monkeypatch)
    sessionmaker, owner, project, job = await _committed_running_job()

    try:
        async with sessionmaker() as worker:
            await jobs_repo.update(
                worker, job_id=job.id, status="running", started_at=datetime.now(UTC), attempts=1
            )
            await worker.rollback()  # the crash: an open transaction is rolled back

        async with sessionmaker() as api:
            added = await topics_service.add_keyword_topics(
                api, project_id=project.id, owner_id=owner.id, keywords=["Respiration"]
            )
            await api.commit()

        async with sessionmaker() as observer:
            after = await jobs_repo.get_by_id(observer, job.id)
        assert [a.topic.title for a in added] == ["Respiration"]
        assert after is not None and after.status == "failed"
        assert after.error == service.STALE_EXTRACTION_ERROR
        [recovery] = [
            call.kwargs
            for call in recorded.await_args_list
            if call.kwargs["action"] == "topics.extraction_recovered"
        ]
        assert recovery["actor_type"] == "system" and recovery["entity_id"] == job.id
    finally:
        await _cleanup(owner)
