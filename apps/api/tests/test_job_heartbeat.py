"""A live long-running job is never reclaimed as stale; a dead one is; a worker that was reclaimed
anyway stops instead of carrying on spending.

Recovery (``jobs.fail_stale_running``) measures a job's silence from the later of its start and the
last progress a worker reported (``payload.progress.updated_at``), so a generation or render that
runs for hours while reporting is safe, and one that goes quiet is reclaimed ``stale_after`` after
its last word. Every long-running handler reports through ``jobs.record_progress``, which refuses
once the job is no longer ``running`` - that refusal is the fence that stops a replaced worker.
The per-service stale tests (``test_stale_*_recovery.py``) cover the enqueue-time behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from edutoon.core.errors import JobAbandonedError
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo
from edutoon.services import evidence as evidence_service
from edutoon.services import job_errors
from edutoon.services import script_audio as script_audio_service
from edutoon.services import scripts as scripts_service
from edutoon.services import storyboard as storyboard_service
from edutoon.services import topic_extraction as topic_extraction_service

pytestmark = pytest.mark.provider_mocked

HOUR = timedelta(hours=1)
# ``now()`` is frozen at the start of the test's one transaction, so a job that reported a moment
# ago is "in the future" of it; a negative threshold reclaims regardless (in production each request
# has its own transaction and this is not needed).
RECLAIM_NOW = timedelta(hours=-1)


async def _project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(db_session, owner_id=owner.id, title="P", source_type="pdf")


def _progress(ago: timedelta | None, *, raw: str | None = None) -> dict[str, Any]:
    stamp = raw if raw is not None else (datetime.now(UTC) - ago).isoformat()  # type: ignore[operator]
    return {"progress": {"stage": "working", "updated_at": stamp}}


async def _running_job(
    db_session,
    project,
    *,
    started_ago: timedelta,
    progress: dict[str, Any] | None = None,
    kind: str = "generate_script",
):
    job = await jobs_repo.create(db_session, project_id=project.id, kind=kind)
    fields: dict[str, Any] = {
        "status": "running",
        "started_at": datetime.now(UTC) - started_ago,
    }
    if progress is not None:
        fields["payload"] = progress
    updated = await jobs_repo.update(db_session, job_id=job.id, **fields)
    assert updated is not None
    return updated


async def _recover(db_session, project, kind: str = "generate_script", stale_after=HOUR):
    return await jobs_repo.fail_stale_running(
        db_session, project_id=project.id, kind=kind, stale_after=stale_after, error="stale"
    )


# --- the heartbeat ----------------------------------------------------------------------------


async def test_a_job_started_long_ago_but_still_reporting_is_not_stale(db_session):
    project = await _project(db_session)
    job = await _running_job(
        db_session,
        project,
        started_ago=timedelta(hours=6),
        progress=_progress(timedelta(minutes=1)),
    )

    assert await _recover(db_session, project) == []
    still = await jobs_repo.get_by_id(db_session, job.id)
    assert still is not None and still.status == "running"


async def test_a_job_that_went_silent_is_stale_from_its_last_progress_not_its_start(db_session):
    project = await _project(db_session)
    job = await _running_job(
        db_session,
        project,
        started_ago=timedelta(hours=6),
        progress=_progress(timedelta(minutes=90)),
    )

    [recovered] = await _recover(db_session, project)
    assert recovered.id == job.id and recovered.status == "failed"


async def test_a_job_that_never_reported_is_stale_from_its_start_as_before(db_session):
    project = await _project(db_session)
    job = await _running_job(db_session, project, started_ago=timedelta(hours=2))

    [recovered] = await _recover(db_session, project)
    assert recovered.id == job.id


async def test_a_job_that_started_recently_is_not_stale_even_without_progress(db_session):
    project = await _project(db_session)
    await _running_job(db_session, project, started_ago=timedelta(minutes=5))

    assert await _recover(db_session, project) == []


async def test_a_progress_stamp_older_than_the_start_does_not_make_a_fresh_job_stale(db_session):
    """A retried job keeps its old payload until it reports: a stamp from before this start must
    not drag the job's last sign of life back."""
    project = await _project(db_session)
    await _running_job(
        db_session,
        project,
        started_ago=timedelta(minutes=5),
        progress=_progress(timedelta(hours=5)),
    )

    assert await _recover(db_session, project) == []


@pytest.mark.parametrize("raw", ["", "yesterday", "not a timestamp", "12:00"])
async def test_a_malformed_progress_stamp_is_ignored_and_never_breaks_recovery(db_session, raw):
    project = await _project(db_session)
    job = await _running_job(
        db_session,
        project,
        started_ago=timedelta(hours=2),
        progress=_progress(None, raw=raw),
    )

    [recovered] = await _recover(db_session, project)  # falls back to its start
    assert recovered.id == job.id


@pytest.mark.parametrize("shape", [{"progress": "text"}, {"progress": None}, {"progress": {}}, {}])
async def test_a_payload_without_a_usable_progress_falls_back_to_the_start(db_session, shape):
    project = await _project(db_session)
    await _running_job(db_session, project, started_ago=timedelta(hours=2), progress=shape)

    assert len(await _recover(db_session, project)) == 1


async def test_the_threshold_is_the_argument(db_session):
    project = await _project(db_session)
    await _running_job(
        db_session,
        project,
        started_ago=timedelta(hours=1),
        progress=_progress(timedelta(minutes=2)),
    )

    assert await _recover(db_session, project, stale_after=timedelta(minutes=10)) == []
    assert len(await _recover(db_session, project, stale_after=timedelta(minutes=1))) == 1


async def test_recovery_only_touches_its_own_kind_and_project(db_session):
    project, other = await _project(db_session), await _project(db_session)
    await _running_job(db_session, project, started_ago=timedelta(hours=2), kind="render_video")
    await _running_job(db_session, other, started_ago=timedelta(hours=2))

    assert await _recover(db_session, project) == []  # a render, not a generation
    assert await _recover(db_session, other, kind="render_video") == []


async def test_recovery_at_enqueue_time_leaves_a_reporting_job_running_and_blocking(db_session):
    project = await _project(db_session)
    job = await _running_job(
        db_session,
        project,
        started_ago=timedelta(hours=3),
        progress=_progress(timedelta(seconds=30)),
        kind="extract_topics",
    )

    from edutoon.core.errors import ConflictError

    with pytest.raises(ConflictError, match="already queued or running"):
        await topic_extraction_service.refuse_if_extraction_active(
            db_session, project_id=project.id
        )
    still = await jobs_repo.get_by_id(db_session, job.id)
    assert still is not None and still.status == "running"  # not reclaimed, so not duplicated


# --- the fence --------------------------------------------------------------------------------


async def test_progress_is_recorded_while_the_job_runs_and_refused_once_it_is_not(db_session):
    project = await _project(db_session)
    job = await _running_job(db_session, project, started_ago=timedelta(minutes=1))

    assert await jobs_repo.record_progress(db_session, job_id=job.id, payload={"a": 1}) is True
    await _recover(db_session, project, stale_after=RECLAIM_NOW)

    assert await jobs_repo.record_progress(db_session, job_id=job.id, payload={"a": 2}) is False
    after = await jobs_repo.get_by_id(db_session, job.id)
    assert after is not None and after.payload == {"a": 1}  # the late write changed nothing


async def test_progress_for_a_job_that_finished_or_never_started_is_refused(db_session):
    project = await _project(db_session)
    queued = await jobs_repo.create(db_session, project_id=project.id, kind="generate_script")

    assert await jobs_repo.record_progress(db_session, job_id=queued.id, payload={}) is False
    assert await jobs_repo.record_progress(db_session, job_id=uuid4(), payload={}) is False


@pytest.mark.parametrize(
    "writer",
    [
        scripts_service._report_progress,
        script_audio_service._report_progress,
        storyboard_service._report_progress,
    ],
    ids=["scripts", "script_audio", "storyboard"],
)
async def test_a_worker_that_was_replaced_stops_at_its_next_progress_report(db_session, writer):
    project = await _project(db_session)
    job = await _running_job(db_session, project, started_ago=timedelta(minutes=1))
    sessionmaker = async_sessionmaker(
        bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )

    await writer(sessionmaker, job_id=job.id, base_payload={}, stage="one")  # alive: recorded
    await _recover(db_session, project, stale_after=RECLAIM_NOW)  # reclaimed meanwhile
    with pytest.raises(JobAbandonedError):
        await writer(sessionmaker, job_id=job.id, base_payload={}, stage="two")


async def test_a_reclaimed_worker_is_told_why_in_words_for_the_owner():
    message = job_errors.public_job_error(JobAbandonedError())
    assert "replaced" in message and "stopped" in message


async def test_a_job_with_no_sessionmaker_reports_nothing_and_cannot_be_fenced(db_session):
    """Direct unit-test callers pass no sessionmaker: they get no progress, and no error."""
    await scripts_service._report_progress(None, job_id=uuid4(), base_payload={}, stage="x")


# --- indexing: heartbeat, fence, and recovery of a dead job -----------------------------------


async def _indexing(db_session, monkeypatch, *, chunks: int = 5, fail_second_batch_with=None):
    from openai import AsyncOpenAI

    from edutoon.domain.embedding_batches import BatchLimits
    from edutoon.providers.embeddings import Embeddings
    from tests.test_evidence_indexing import RecordingCreate, _make_source, _queue

    project, source, _ = await _make_source(db_session, chunks)
    job = await _queue(db_session, project, source)
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")
    create = RecordingCreate()
    monkeypatch.setattr(client.embeddings, "create", create)
    embeddings = Embeddings(
        client, model="text-embedding-3-small", limits=BatchLimits(max_inputs=2)
    )
    return project, source, job, embeddings, create


async def test_indexing_reports_progress_batch_by_batch_which_is_its_heartbeat(
    db_session, monkeypatch
):
    _project_, _source, job, embeddings, _create = await _indexing(db_session, monkeypatch)

    done = await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=job.id)

    assert done.status == "succeeded"
    progress = done.payload["progress"]
    assert (progress["stage"], progress["chunks_embedded"], progress["chunks_total"]) == (
        "embedding",
        5,
        5,
    )
    assert datetime.fromisoformat(progress["updated_at"]) > datetime.now(UTC) - timedelta(minutes=1)


async def test_an_indexing_job_that_was_reclaimed_stops_and_keeps_only_whole_batches(
    db_session, monkeypatch
):
    from tests.test_evidence_indexing import _missing

    project, source, job, embeddings, create = await _indexing(db_session, monkeypatch)
    real_call = create.__call__

    async def reclaimed_after_the_first_batch(**kwargs: Any):
        if len(create.requests) == 1:  # the second request: recovery took the job away
            await jobs_repo.update(db_session, job_id=job.id, status="failed", error="reclaimed")
        return await real_call(**kwargs)

    monkeypatch.setattr(embeddings._client.embeddings, "create", reclaimed_after_the_first_batch)

    done = await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=job.id)

    assert done.status == "failed" and "replaced" in done.error
    assert len(create.requests) == 2  # it stopped: no third request was made
    assert await _missing(db_session, source) == [
        2,
        3,
        4,
    ]  # batch 1 kept; the fenced batch 2 is not


async def test_a_dead_indexing_job_no_longer_blocks_its_own_retry(db_session, monkeypatch):
    project, source, job, _embeddings, _create = await _indexing(db_session, monkeypatch)
    await jobs_repo.update(
        db_session,
        job_id=job.id,
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=3),
    )

    requeued = await evidence_service.retry_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    assert requeued.id == job.id and requeued.status == "queued"


async def test_a_live_indexing_job_still_blocks_a_retry(db_session, monkeypatch):
    from edutoon.core.errors import ConflictError

    project, source, job, _embeddings, _create = await _indexing(db_session, monkeypatch)
    await jobs_repo.update(
        db_session,
        job_id=job.id,
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=3),
        payload={**job.payload, **_progress(timedelta(seconds=10))},
    )

    with pytest.raises(ConflictError, match="already being indexed"):
        await evidence_service.retry_indexing_job(
            db_session, project_id=project.id, source_id=source.id
        )


async def test_a_render_that_was_replaced_stops_at_its_next_progress_report(db_session):
    from edutoon.domain import render as render_domain
    from edutoon.services import render as render_service

    plan = render_domain.build_render_plan(
        [
            render_domain.SegmentInput(
                "s0",
                0,
                3_000,
                "h",
                [render_domain.SceneInput(0, 0, 3_000, "title_card", {"title": "T"})],
            )
        ],
        orientation="horizontal",
        quality="low",
        encoder="libx264",
    )
    project = await _project(db_session)
    job = await _running_job(
        db_session, project, started_ago=timedelta(minutes=1), kind="render_video"
    )
    sessionmaker = async_sessionmaker(
        bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    progress = render_service._Progress(sessionmaker, job.id, {}, plan, {"s0": 1})

    await progress.report("rendering")  # alive: recorded
    await _recover(db_session, project, kind="render_video", stale_after=RECLAIM_NOW)
    with pytest.raises(JobAbandonedError):
        await progress.report("assembling")
