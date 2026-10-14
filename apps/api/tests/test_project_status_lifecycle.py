"""``projects.status`` follows the project's pipeline (repository level).

The rule itself is ``domain.project_status``; ``projects.refresh_status`` reads a project's
jobs and sources and stores the answer. It is called from ``repositories.jobs`` and
``repositories.uploaded_sources`` at the moments the pipeline changes state, so every path
that changes a job - an API request, the worker, crash recovery - moves the status without
its own call. These tests drive those repository functions directly. The same behaviour
through the HTTP API and the worker is covered in ``test_uploaded_sources_api.py``,
``test_topics_api.py``, ``test_scripts_api.py`` and ``test_worker.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo


def _now() -> datetime:
    """The clock *when asked*. A module-level ``NOW`` was read at import, so on a long suite it was
    hours old and the job ``test_recovery_with_nothing_to_recover`` made with ``started_at=NOW``
    counted as stale (older than the recovery window): the flake. The guard is
    ``test_isolation_of_test_assertions``."""
    return datetime.now(UTC)


async def _project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session, owner_id=owner.id, title="Photosynthesis", source_type="pdf"
    )


async def _status(db_session, project) -> str:
    fresh = await projects_repo.get_by_id(db_session, project.id)
    assert fresh is not None
    return fresh.status


async def _finish(db_session, job, status: str, *, at: datetime | None = None):
    """End ``job`` as ``status`` ("succeeded" / "failed" / "cancelled") at ``at``."""
    finished = at or datetime.now(UTC)
    updated = await jobs_repo.update(
        db_session,
        job_id=job.id,
        status=status,
        started_at=finished - timedelta(minutes=1),
        finished_at=finished,
    )
    assert updated is not None
    return updated


async def _source(db_session, project):
    return await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket="b",
        storage_key=f"projects/{project.id}/{uuid4().hex}.pdf",
        byte_size=10,
        checksum_sha256=uuid4().hex + uuid4().hex,
    )


# --- the lifecycle --------------------------------------------------------------------


async def test_a_new_project_is_a_draft(db_session):
    project = await _project(db_session)

    assert project.status == "draft"


async def test_queueing_a_job_makes_the_project_processing(db_session):
    project = await _project(db_session)

    await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")

    assert await _status(db_session, project) == "processing"


async def test_a_job_that_is_claimed_and_running_keeps_the_project_processing(db_session):
    project = await _project(db_session)
    queued = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")

    claimed = await jobs_repo.claim_next(db_session)
    assert claimed is not None and claimed.id == queued.id
    running = await jobs_repo.update(db_session, job_id=queued.id, status="running", attempts=1)

    assert running is not None
    assert await _status(db_session, project) == "processing"


async def test_a_job_that_succeeds_leaves_the_project_ready(db_session):
    project = await _project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")

    await _finish(db_session, job, "succeeded")

    assert await _status(db_session, project) == "ready"


async def test_a_job_that_fails_leaves_the_project_failed(db_session):
    project = await _project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="generate_script")

    await _finish(db_session, job, "failed")

    assert await _status(db_session, project) == "failed"


async def test_the_project_stays_processing_until_every_job_has_ended(db_session):
    project = await _project(db_session)
    first = await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")
    second = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")

    await _finish(db_session, first, "succeeded")
    assert await _status(db_session, project) == "processing"

    await _finish(db_session, second, "succeeded")
    assert await _status(db_session, project) == "ready"


async def test_retrying_a_failed_project_takes_it_back_through_processing_to_ready(db_session):
    project = await _project(db_session)
    failed = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    await _finish(db_session, failed, "failed", at=_now() - timedelta(minutes=10))
    assert await _status(db_session, project) == "failed"

    retry = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    assert await _status(db_session, project) == "processing"

    await _finish(db_session, retry, "succeeded", at=_now())
    assert await _status(db_session, project) == "ready"


async def test_more_work_after_a_success_puts_a_ready_project_back_to_processing(db_session):
    project = await _project(db_session)
    done = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    await _finish(db_session, done, "succeeded")
    assert await _status(db_session, project) == "ready"

    await jobs_repo.create(db_session, project_id=project.id, kind="generate_script")

    assert await _status(db_session, project) == "processing"


# --- "latest" means the most recently finished ----------------------------------------


async def test_the_outcome_that_finished_last_decides_not_the_one_created_last(db_session):
    project = await _project(db_session)
    slow = await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")
    fast = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")

    # The later-created job finishes first (a failure); the earlier-created, slower one
    # finishes after it and succeeds. The project's latest outcome is that success.
    await _finish(db_session, fast, "failed", at=_now() - timedelta(minutes=5))
    await _finish(db_session, slow, "succeeded", at=_now())

    assert await _status(db_session, project) == "ready"


async def test_a_failure_that_finishes_after_a_success_decides(db_session):
    project = await _project(db_session)
    ok = await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")
    bad = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")

    await _finish(db_session, ok, "succeeded", at=_now() - timedelta(minutes=5))
    await _finish(db_session, bad, "failed", at=_now())

    assert await _status(db_session, project) == "failed"


async def test_a_cancelled_job_is_not_an_outcome(db_session):
    project = await _project(db_session)
    only = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")

    await _finish(db_session, only, "cancelled")
    assert await _status(db_session, project) == "draft"

    ok = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    await _finish(db_session, ok, "succeeded", at=_now() - timedelta(minutes=5))
    later = await jobs_repo.create(db_session, project_id=project.id, kind="generate_script")
    await _finish(db_session, later, "cancelled", at=_now())
    assert await _status(db_session, project) == "ready"


async def test_every_job_kind_counts_alike(db_session):
    """A stage added later (voice, render) needs no change to the rule."""
    project = await _project(db_session)

    job = await jobs_repo.create(db_session, project_id=project.id, kind="render_video")
    assert await _status(db_session, project) == "processing"

    await _finish(db_session, job, "failed")
    assert await _status(db_session, project) == "failed"


# --- a PDF that cannot be read has no job, but is still a failure ---------------------


async def test_a_pdf_that_fails_to_parse_fails_the_project(db_session):
    project = await _project(db_session)
    source = await _source(db_session, project)

    await uploaded_sources_repo.update(
        db_session, source_id=source.id, project_id=project.id, status="failed"
    )

    assert await _status(db_session, project) == "failed"


async def test_a_pdf_that_parses_does_not_by_itself_change_the_status(db_session):
    """Parsing succeeding is followed by a ``build_evidence`` job, which is what moves it."""
    project = await _project(db_session)
    source = await _source(db_session, project)

    await uploaded_sources_repo.update(
        db_session, source_id=source.id, project_id=project.id, status="parsed", page_count=3
    )

    assert await _status(db_session, project) == "draft"


async def test_a_later_success_supersedes_an_earlier_unreadable_pdf(db_session):
    project = await _project(db_session)
    bad = await _source(db_session, project)
    await uploaded_sources_repo.update(
        db_session, source_id=bad.id, project_id=project.id, status="failed"
    )
    assert await _status(db_session, project) == "failed"

    job = await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")
    await _finish(db_session, job, "succeeded", at=datetime.now(UTC) + timedelta(seconds=1))

    assert await _status(db_session, project) == "ready"


async def test_an_unreadable_pdf_after_a_success_is_the_latest_outcome(db_session):
    project = await _project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")
    await _finish(db_session, job, "succeeded", at=_now() - timedelta(minutes=5))
    assert await _status(db_session, project) == "ready"

    bad = await _source(db_session, project)
    await uploaded_sources_repo.update(
        db_session, source_id=bad.id, project_id=project.id, status="failed"
    )

    assert await _status(db_session, project) == "failed"


# --- crash recovery -------------------------------------------------------------------


async def test_a_job_failed_by_crash_recovery_fails_the_project(db_session):
    project = await _project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    await jobs_repo.update(
        db_session,
        job_id=job.id,
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    assert await _status(db_session, project) == "processing"

    recovered = await jobs_repo.fail_stale_running(
        db_session,
        project_id=project.id,
        kind="extract_topics",
        stale_after=timedelta(hours=1),
        error="The worker stopped; run the extraction again.",
    )

    assert [item.id for item in recovered] == [job.id]
    assert await _status(db_session, project) == "failed"


async def test_recovery_with_nothing_to_recover_changes_nothing(db_session):
    project = await _project(db_session)
    job = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    await jobs_repo.update(db_session, job_id=job.id, status="running", started_at=_now())

    recovered = await jobs_repo.fail_stale_running(
        db_session,
        project_id=project.id,
        kind="extract_topics",
        stale_after=timedelta(hours=1),
        error="unused",
    )

    assert recovered == []
    assert await _status(db_session, project) == "processing"


# --- archived, and other projects -----------------------------------------------------


async def test_an_archived_project_stays_archived_through_every_transition(db_session):
    project = await _project(db_session)
    await projects_repo.update(
        db_session, project_id=project.id, owner_id=project.owner_id, status="archived"
    )

    job = await jobs_repo.create(db_session, project_id=project.id, kind="extract_topics")
    assert await _status(db_session, project) == "archived"

    await _finish(db_session, job, "failed")
    assert await _status(db_session, project) == "archived"


async def test_one_projects_jobs_never_move_another_projects_status(db_session):
    mine = await _project(db_session)
    theirs = await _project(db_session)

    job = await jobs_repo.create(db_session, project_id=mine.id, kind="extract_topics")
    await _finish(db_session, job, "failed")

    assert await _status(db_session, mine) == "failed"
    assert await _status(db_session, theirs) == "draft"


async def test_refreshing_a_project_that_no_longer_exists_is_harmless(db_session):
    assert await projects_repo.refresh_status(db_session, project_id=uuid4()) is None
