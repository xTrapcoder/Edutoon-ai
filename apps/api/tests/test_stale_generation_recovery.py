"""Stale-generation recovery for ``generate_script`` (mirrors
``test_stale_extraction_recovery.py``).

``run_generate_script_job`` commits its "running" transition immediately and
releases the job row's lock before the (potentially long) generation work
begins - exactly like ``run_extract_topics_job`` / ``run_extract_claims_job``.
Unlike those two, ``enqueue_script_generation`` never called
``jobs_repo.fail_stale_running`` for its own job kind, so a worker that
crashed mid-generation left its job ``running`` forever with nothing to
recover it - and its ``scripts`` row stuck ``pending`` forever, since nothing
else ever moves a ``pending`` script on. This file proves both halves fixed:
the job is recovered, and its stranded script is failed alongside it, without
touching topic/claim extraction recovery (already covered by its own tests)
or generation itself (covered by ``test_services_scripts.py``).
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
from edutoon.db.database import dispose_engine, get_engine
from edutoon.db.session import get_sessionmaker
from edutoon.db.tables import users as users_table
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import scripts as service
from tests.storage_isolation import UPLOADS_BUCKET

KIND = "generate_script"
SCRIPT_MODEL = "claude-sonnet-5"


async def _project_with_supported_topic(db_session):
    """A project with one accepted topic that has a supported claim - the
    minimum ``enqueue_script_generation`` needs to succeed. Never touches the
    LLM: enqueue itself makes no provider call.
    """
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Photosynthesis", source_type="pdf"
    )
    topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="A topic", status="accepted"
    )
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket=UPLOADS_BUCKET,
        storage_key=f"{project.id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    [chunk] = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=0, content="A fact."
            )
        ],
    )
    await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="A fact.",
        status="supported",
        embedding_model="text-embedding-3-small",
        llm_model=SCRIPT_MODEL,
        topic_id=topic.id,
        citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.9)],
    )
    return owner, project


async def _stale_generation(
    db_session, project, *, started_ago: timedelta | None = timedelta(hours=2)
):
    """A ``pending`` script plus its ``generate_script`` job, ``running`` since
    ``started_ago`` - the shape ``enqueue_script_generation`` itself creates.
    """
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=SCRIPT_MODEL)
    job = await jobs_repo.create(
        db_session,
        project_id=project.id,
        kind=KIND,
        payload={"script_id": str(script.id), "project_id": str(project.id), "topic_ids": []},
    )
    fields: dict[str, Any] = {"status": "running"}
    if started_ago is not None:
        fields["started_at"] = datetime.now(UTC) - started_ago
    updated = await jobs_repo.update(db_session, job_id=job.id, **fields)
    assert updated is not None
    return script, updated


async def _job_status(session, job_id) -> str:
    job = await jobs_repo.get_by_id(session, job_id)
    assert job is not None
    return job.status


async def _script_status(session, project_id, script_id) -> str:
    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    assert script is not None
    return script.status


def _with_threshold(monkeypatch: Any, seconds: int) -> None:
    settings = get_settings().model_copy(update={"SCRIPT_GENERATION_STALE_AFTER_SECONDS": seconds})
    monkeypatch.setattr(service, "get_settings", lambda: settings)


async def _recovered_audits(session, project) -> list[Any]:
    page = await audit_logs_service.list_audit_logs_for_project(session, project.id)
    return [e for e in page.items if e.action == "script.generation_recovered"]


# --- recovery: an abandoned job stops blocking / stops stranding its script ----------------


async def test_an_abandoned_generation_no_longer_blocks_a_new_one(db_session):
    owner, project = await _project_with_supported_topic(db_session)
    stale_script, stale_job = await _stale_generation(db_session, project)

    fresh = await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="again"
    )

    assert fresh.replayed is False
    assert fresh.job.id != stale_job.id and fresh.script.id != stale_script.id
    assert fresh.job.status == "queued"
    assert await _job_status(db_session, stale_job.id) == "failed"


async def test_the_stranded_script_is_failed_alongside_its_job(db_session):
    owner, project = await _project_with_supported_topic(db_session)
    stale_script, stale_job = await _stale_generation(db_session, project)

    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="again"
    )

    assert await _script_status(db_session, project.id, stale_script.id) == "failed"


async def test_a_recovered_job_is_a_normal_failed_job_and_says_what_happened(db_session):
    owner, project = await _project_with_supported_topic(db_session)
    _, stale_job = await _stale_generation(db_session, project)

    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    after = await jobs_repo.get_by_id(db_session, stale_job.id)
    assert after is not None
    assert after.status == "failed"
    assert after.error == service.STALE_GENERATION_ERROR
    assert after.finished_at is not None and after.started_at == stale_job.started_at
    assert after.attempts == stale_job.attempts  # recovery is not an attempt


async def test_recovery_is_audited_as_a_system_action(db_session):
    owner, project = await _project_with_supported_topic(db_session)
    _, stale_job = await _stale_generation(db_session, project)

    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    [entry] = await _recovered_audits(db_session, project)
    assert entry.actor_type == "system" and entry.actor_id is None
    assert entry.entity_id == stale_job.id
    assert entry.context["job_id"] == str(stale_job.id)
    assert (
        entry.context["stale_after_seconds"] == get_settings().SCRIPT_GENERATION_STALE_AFTER_SECONDS
    )


async def test_a_running_job_with_no_started_at_is_recovered_from_its_creation_time(db_session):
    """Never expected, but a recovery that violated the finished-needs-started
    CHECK would fail the enqueue instead of clearing the block.
    """
    from edutoon.db.tables import jobs as jobs_table

    owner, project = await _project_with_supported_topic(db_session)
    _, stale_job = await _stale_generation(db_session, project, started_ago=None)
    await db_session.execute(
        jobs_table.update()
        .where(jobs_table.c.id == stale_job.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=2))
    )

    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    after = await jobs_repo.get_by_id(db_session, stale_job.id)
    assert after is not None and after.status == "failed"
    assert after.started_at is not None and after.finished_at is not None


# --- no premature recovery ------------------------------------------------------------------


async def test_a_recently_started_generation_is_left_running(db_session):
    owner, project = await _project_with_supported_topic(db_session)
    _, running = await _stale_generation(db_session, project, started_ago=timedelta(minutes=5))

    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    assert await _job_status(db_session, running.id) == "running"
    assert await _recovered_audits(db_session, project) == []


async def test_the_threshold_is_the_setting(db_session, monkeypatch):
    _with_threshold(monkeypatch, 60)
    owner, project = await _project_with_supported_topic(db_session)
    _, young = await _stale_generation(db_session, project, started_ago=timedelta(seconds=30))

    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k1"
    )
    assert await _job_status(db_session, young.id) == "running"

    _, old = await _stale_generation(db_session, project, started_ago=timedelta(seconds=120))
    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k2"
    )
    assert await _job_status(db_session, old.id) == "failed"
    assert await _job_status(db_session, young.id) == "running"


async def test_a_queued_job_is_never_treated_as_abandoned(db_session):
    owner, project = await _project_with_supported_topic(db_session)
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=SCRIPT_MODEL)
    queued = await jobs_repo.create(
        db_session,
        project_id=project.id,
        kind=KIND,
        payload={"script_id": str(script.id), "project_id": str(project.id), "topic_ids": []},
    )
    from edutoon.db.tables import jobs as jobs_table

    await db_session.execute(
        jobs_table.update()
        .where(jobs_table.c.id == queued.id)
        .values(
            created_at=datetime.now(UTC) - timedelta(days=3),
            scheduled_at=datetime.now(UTC) - timedelta(days=3),
        )
    )

    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    assert await _job_status(db_session, queued.id) == "queued"


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
async def test_finished_jobs_are_left_exactly_as_they_are(db_session, status):
    owner, project = await _project_with_supported_topic(db_session)
    script, done = await _stale_generation(db_session, project, started_ago=timedelta(days=1))
    fields: dict[str, Any] = {"status": status}
    if status in ("succeeded", "failed"):
        fields["finished_at"] = datetime.now(UTC)
    done = await jobs_repo.update(db_session, job_id=done.id, **fields)
    assert done is not None

    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    after = await jobs_repo.get_by_id(db_session, done.id)
    assert after == done
    assert await _recovered_audits(db_session, project) == []


async def test_only_generate_script_jobs_of_this_project_are_recovered(db_session):
    owner, project = await _project_with_supported_topic(db_session)
    other_owner, other_project = await _project_with_supported_topic(db_session)

    topics_job = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    topics_job = await jobs_repo.update(
        db_session,
        job_id=topics_job.id,
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    assert topics_job is not None
    _, elsewhere_job = await _stale_generation(db_session, other_project)

    await service.enqueue_script_generation(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="k"
    )

    assert await _job_status(db_session, topics_job.id) == "running"
    assert await _job_status(db_session, elsewhere_job.id) == "running"


# --- the setting -----------------------------------------------------------------------


def test_the_stale_timeout_is_optional_and_defaults_above_the_worst_model_call():
    default = Settings.model_fields["SCRIPT_GENERATION_STALE_AFTER_SECONDS"].default
    assert default >= 3 * 600 + 60


def test_a_timeout_short_enough_to_race_a_healthy_worker_is_rejected_at_start_up():
    with pytest.raises(PydanticValidationError):
        Settings(SCRIPT_GENERATION_STALE_AFTER_SECONDS=59)


# --- live vs crashed worker: two real connections -------------------------------------------


async def _committed_running_generation():
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as setup:
        owner, project = await _project_with_supported_topic(setup)
        script, job = await _stale_generation(setup, project)
        await setup.commit()
    return sessionmaker, owner, project, script, job


async def _cleanup(owner, project) -> None:
    """Deleting the user cascades through projects -> uploaded_sources ->
    source_chunks, but ``claim_citations.source_chunk_id`` is
    ``ON DELETE RESTRICT`` (evidence is never silently orphaned-away) - so the
    claim (and its citations, ``ON DELETE CASCADE`` from there) must go first.
    """
    from edutoon.db.tables import claim_verifications as claim_verifications_table

    async with get_engine().begin() as cleanup:
        await cleanup.execute(
            claim_verifications_table.delete().where(
                claim_verifications_table.c.project_id == project.id
            )
        )
        await cleanup.execute(users_table.delete().where(users_table.c.id == owner.id))
    await dispose_engine()
    session_module._sessionmaker = None


def _quiet_audit(monkeypatch: Any) -> AsyncMock:
    recorded = AsyncMock()
    monkeypatch.setattr(audit_logs_service, "record_audit_log", recorded)
    return recorded


async def test_a_job_a_live_worker_is_running_is_never_recovered_however_old(monkeypatch):
    """The worker's handler marks the job running inside the transaction it
    keeps open for the whole run, so the row stays locked - even past the
    threshold, even though ``enqueue_script_generation`` never refuses a
    second concurrent generation the way extraction does.
    """
    recorded = _quiet_audit(monkeypatch)
    sessionmaker, owner, project, script, job = await _committed_running_generation()

    try:
        async with sessionmaker() as worker, sessionmaker() as api:
            await jobs_repo.update(
                worker, job_id=job.id, status="running", started_at=datetime.now(UTC), attempts=1
            )

            await service.enqueue_script_generation(
                api, project_id=project.id, owner_id=owner.id, idempotency_key="k"
            )
            await api.commit()

            async with sessionmaker() as observer:
                assert await _job_status(observer, job.id) == "running"
            # enqueue_script_generation audits its own success; only recovery is asserted absent.
            assert not any(
                call.kwargs["action"] == "script.generation_recovered"
                for call in recorded.await_args_list
            )

            await jobs_repo.update(
                worker, job_id=job.id, status="succeeded", finished_at=datetime.now(UTC), result={}
            )
            await worker.commit()
    finally:
        await _cleanup(owner, project)


async def test_a_job_whose_worker_crashed_is_recovered(monkeypatch):
    recorded = _quiet_audit(monkeypatch)
    sessionmaker, owner, project, script, job = await _committed_running_generation()

    try:
        async with sessionmaker() as worker:
            await jobs_repo.update(
                worker, job_id=job.id, status="running", started_at=datetime.now(UTC), attempts=1
            )
            await worker.rollback()  # the crash: an open transaction is rolled back

        async with sessionmaker() as api:
            await service.enqueue_script_generation(
                api, project_id=project.id, owner_id=owner.id, idempotency_key="k"
            )
            await api.commit()

        async with sessionmaker() as observer:
            after = await jobs_repo.get_by_id(observer, job.id)
            after_script = await scripts_repo.get_by_id_for_project(observer, script.id, project.id)
        assert after is not None and after.status == "failed"
        assert after.error == service.STALE_GENERATION_ERROR
        assert after_script is not None and after_script.status == "failed"
        [recovery] = [
            call.kwargs
            for call in recorded.await_args_list
            if call.kwargs["action"] == "script.generation_recovered"
        ]
        assert recovery["actor_type"] == "system" and recovery["entity_id"] == job.id
    finally:
        await _cleanup(owner, project)
