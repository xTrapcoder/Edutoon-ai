"""Stale-synthesis recovery for ``synthesise_voice`` (mirrors
``test_stale_generation_recovery.py`` / ``test_stale_extraction_recovery.py``).

``run_synthesise_voice_job`` commits its "running" transition immediately and
releases the job row's lock before synthesis begins - the same shape as
``generate_script`` and the extraction jobs. Unlike ``generate_script``,
synthesis never changes the script's own status, so there is nothing else to
repair when a worker crashes mid-run: already-``ready`` segment audio is
simply left alone, and a fresh ``enqueue_synthesis`` call resumes it.
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
from edutoon.db.tables import users as users_table
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.scripts import NewScriptSegment
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import script_audio as service

KIND = "synthesise_voice"
MODEL = "claude-sonnet-5"


async def _approved_script(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="topic"
    )
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(segment_index=0, narration_text="Hi.", estimated_duration_ms=500)
        ],
    )
    approved = await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status="approved"
    )
    assert approved is not None
    return owner, project, approved


async def _stale_job(
    db_session, project, script, *, started_ago: timedelta | None = timedelta(hours=2)
):
    job = await jobs_repo.create(
        db_session,
        project_id=project.id,
        kind=KIND,
        payload={"script_id": str(script.id), "project_id": str(project.id)},
    )
    fields: dict[str, Any] = {"status": "running"}
    if started_ago is not None:
        fields["started_at"] = datetime.now(UTC) - started_ago
    updated = await jobs_repo.update(db_session, job_id=job.id, **fields)
    assert updated is not None
    return updated


async def _job_status(session, job_id) -> str:
    job = await jobs_repo.get_by_id(session, job_id)
    assert job is not None
    return job.status


def _with_threshold(monkeypatch: Any, seconds: int) -> None:
    settings = get_settings().model_copy(update={"SYNTHESIS_STALE_AFTER_SECONDS": seconds})
    monkeypatch.setattr(service, "get_settings", lambda: settings)


async def _recovered_audits(session, project) -> list[Any]:
    page = await audit_logs_service.list_audit_logs_for_project(session, project.id)
    return [e for e in page.items if e.action == "synthesis.recovered"]


# --- recovery: an abandoned job stops blocking -----------------------------------------------


async def test_an_abandoned_synthesis_no_longer_blocks_a_new_one(db_session):
    owner, project, script = await _approved_script(db_session)
    stale = await _stale_job(db_session, project, script)

    fresh = await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        script_id=script.id,
        idempotency_key="again",
    )

    assert fresh.replayed is False and fresh.job.id != stale.id
    assert fresh.job.status == "queued"
    assert await _job_status(db_session, stale.id) == "failed"


async def test_a_recovered_job_is_a_normal_failed_job_and_says_what_happened(db_session):
    owner, project, script = await _approved_script(db_session)
    stale = await _stale_job(db_session, project, script)

    await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        script_id=script.id,
        idempotency_key="k",
    )

    after = await jobs_repo.get_by_id(db_session, stale.id)
    assert after is not None
    assert after.status == "failed"
    assert after.error == service.STALE_SYNTHESIS_ERROR
    assert after.finished_at is not None and after.started_at == stale.started_at
    assert after.attempts == stale.attempts  # recovery is not an attempt


async def test_recovery_is_audited_as_a_system_action(db_session):
    owner, project, script = await _approved_script(db_session)
    stale = await _stale_job(db_session, project, script)

    await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        script_id=script.id,
        idempotency_key="k",
    )

    [entry] = await _recovered_audits(db_session, project)
    assert entry.actor_type == "system" and entry.actor_id is None
    assert entry.entity_id == stale.id
    assert entry.context["job_id"] == str(stale.id)
    assert entry.context["stale_after_seconds"] == get_settings().SYNTHESIS_STALE_AFTER_SECONDS


# --- no premature recovery ---------------------------------------------------------------------


async def test_a_recently_started_synthesis_is_left_running_and_still_blocks(db_session):
    owner, project, script = await _approved_script(db_session)
    running = await _stale_job(db_session, project, script, started_ago=timedelta(minutes=5))

    with pytest.raises(ConflictError, match="already queued or running"):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=owner.id,
            script_id=script.id,
            idempotency_key="k",
        )

    assert await _job_status(db_session, running.id) == "running"
    assert await _recovered_audits(db_session, project) == []


async def test_the_threshold_is_the_setting(db_session, monkeypatch):
    _with_threshold(monkeypatch, 60)
    owner, project, script = await _approved_script(db_session)
    young = await _stale_job(db_session, project, script, started_ago=timedelta(seconds=30))

    with pytest.raises(ConflictError, match="already queued or running"):
        await service.enqueue_synthesis(
            db_session,
            project_id=project.id,
            owner_id=owner.id,
            script_id=script.id,
            idempotency_key="k1",
        )
    assert await _job_status(db_session, young.id) == "running"


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
async def test_finished_jobs_are_left_exactly_as_they_are(db_session, status):
    owner, project, script = await _approved_script(db_session)
    done = await _stale_job(db_session, project, script, started_ago=timedelta(days=1))
    fields: dict[str, Any] = {"status": status}
    if status in ("succeeded", "failed"):
        fields["finished_at"] = datetime.now(UTC)
    done = await jobs_repo.update(db_session, job_id=done.id, **fields)
    assert done is not None

    await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        script_id=script.id,
        idempotency_key="k",
    )

    after = await jobs_repo.get_by_id(db_session, done.id)
    assert after == done
    assert await _recovered_audits(db_session, project) == []


async def test_only_synthesise_voice_jobs_of_this_project_are_recovered(db_session):
    owner, project, script = await _approved_script(db_session)
    other_owner, other_project, other_script = await _approved_script(db_session)

    generation_job = await jobs_repo.create(
        db_session, project_id=project.id, kind="generate_script"
    )
    generation_job = await jobs_repo.update(
        db_session,
        job_id=generation_job.id,
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    assert generation_job is not None
    elsewhere_job = await _stale_job(db_session, other_project, other_script)

    await service.enqueue_synthesis(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        script_id=script.id,
        idempotency_key="k",
    )

    assert await _job_status(db_session, generation_job.id) == "running"
    assert await _job_status(db_session, elsewhere_job.id) == "running"


# --- the setting -----------------------------------------------------------------------------


def test_the_stale_timeout_is_optional_and_defaults_generously():
    default = Settings.model_fields["SYNTHESIS_STALE_AFTER_SECONDS"].default
    assert default >= 60


def test_a_timeout_short_enough_to_race_a_healthy_worker_is_rejected_at_start_up():
    with pytest.raises(PydanticValidationError):
        Settings(SYNTHESIS_STALE_AFTER_SECONDS=59)


# --- live vs crashed worker: two real connections ---------------------------------------------


async def _committed_running_synthesis():
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as setup:
        owner, project, script = await _approved_script(setup)
        job = await _stale_job(setup, project, script)
        await setup.commit()
    return sessionmaker, owner, project, script, job


async def _cleanup(owner) -> None:
    """``_approved_script`` never creates claim_verifications/source_chunks
    (synthesis doesn't need supported claims the way generation does), so -
    unlike the generation stale-recovery tests - deleting the user alone
    cascades cleanly through project -> script -> script_segments.
    """
    async with get_engine().begin() as cleanup:
        await cleanup.execute(users_table.delete().where(users_table.c.id == owner.id))
    await dispose_engine()
    session_module._sessionmaker = None


def _quiet_audit(monkeypatch: Any) -> AsyncMock:
    recorded = AsyncMock()
    monkeypatch.setattr(audit_logs_service, "record_audit_log", recorded)
    return recorded


async def test_a_job_a_live_worker_is_running_is_never_recovered_however_old(monkeypatch):
    recorded = _quiet_audit(monkeypatch)
    sessionmaker, owner, project, script, job = await _committed_running_synthesis()

    try:
        async with sessionmaker() as worker, sessionmaker() as api:
            await jobs_repo.update(
                worker, job_id=job.id, status="running", started_at=datetime.now(UTC), attempts=1
            )

            with pytest.raises(ConflictError, match="already queued or running"):
                await service.enqueue_synthesis(
                    api,
                    project_id=project.id,
                    owner_id=owner.id,
                    script_id=script.id,
                    idempotency_key="k",
                )
            await api.rollback()

            async with sessionmaker() as observer:
                assert await _job_status(observer, job.id) == "running"
            recorded.assert_not_called()

            await jobs_repo.update(
                worker, job_id=job.id, status="succeeded", finished_at=datetime.now(UTC), result={}
            )
            await worker.commit()
    finally:
        await _cleanup(owner)


async def test_a_job_whose_worker_crashed_is_recovered(monkeypatch):
    recorded = _quiet_audit(monkeypatch)
    sessionmaker, owner, project, script, job = await _committed_running_synthesis()

    try:
        async with sessionmaker() as worker:
            await jobs_repo.update(
                worker, job_id=job.id, status="running", started_at=datetime.now(UTC), attempts=1
            )
            await worker.rollback()

        async with sessionmaker() as api:
            await service.enqueue_synthesis(
                api,
                project_id=project.id,
                owner_id=owner.id,
                script_id=script.id,
                idempotency_key="k",
            )
            await api.commit()

        async with sessionmaker() as observer:
            after = await jobs_repo.get_by_id(observer, job.id)
        assert after is not None and after.status == "failed"
        assert after.error == service.STALE_SYNTHESIS_ERROR
        [recovery] = [
            call.kwargs
            for call in recorded.await_args_list
            if call.kwargs["action"] == "synthesis.recovered"
        ]
        assert recovery["actor_type"] == "system" and recovery["entity_id"] == job.id
    finally:
        await _cleanup(owner)
