"""Evidence Engine - Phase B: embedding generation only.

Retrieval, similarity search, claim verification, and everything past them
are later phases and deliberately not touched here.

Indexing is jobs-backed (``kind='build_evidence'``): ``enqueue_indexing_job``
creates the row, ``run_build_evidence_job`` is what a future worker would
call after dequeuing it. No worker/poller process exists yet - out of scope
for this phase - so today these are called directly (see the tests).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.providers.embeddings import Embeddings
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories.jobs import JobRecord

BUILD_EVIDENCE_JOB_KIND = "build_evidence"


async def enqueue_indexing_job(
    session: AsyncSession, *, project_id: UUID, source_id: UUID
) -> JobRecord:
    """Queue a ``build_evidence`` job for ``source_id``.

    The idempotency key is the source id itself, so a source can only ever
    have one indexing job - a second attempt hits the existing
    ``uq_jobs_project_id_kind_idempotency_key`` unique index and raises
    :class:`~edutoon.core.errors.ConflictError`, rather than silently
    double-queueing (and double-billing) the same work.
    """
    return await jobs_repo.create(
        session,
        project_id=project_id,
        kind=BUILD_EVIDENCE_JOB_KIND,
        payload={"source_id": str(source_id)},
        idempotency_key=str(source_id),
    )


async def index_source(
    session: AsyncSession, embeddings: Embeddings, *, source_id: UUID
) -> int:
    """Embed every chunk of ``source_id`` that doesn't have one yet.

    Returns the number of chunks embedded. Idempotent by construction: a
    re-run (e.g. after a prior partial failure) only ever touches chunks
    still missing an embedding, never re-embeds ones that already have one.
    """
    chunks = await source_chunks_repo.list_missing_embedding_by_source(session, source_id)
    if not chunks:
        return 0

    vectors = await embeddings.embed([chunk.content for chunk in chunks])
    for chunk, vector in zip(chunks, vectors, strict=True):
        await source_chunks_repo.update_embedding(
            session, chunk_id=chunk.id, embedding=vector, embedding_model=embeddings.model
        )
    return len(chunks)


async def run_build_evidence_job(
    session: AsyncSession, embeddings: Embeddings, *, job_id: UUID
) -> JobRecord:
    """Process one queued ``build_evidence`` job end to end: marks it
    running, indexes the source named in its payload, marks it
    succeeded (with the chunk count) or failed (with the error message).

    This is the function a future worker would call immediately after
    dequeuing the job - building that worker/poller is out of scope for
    this phase.
    """
    job = await jobs_repo.get_by_id(session, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")

    running = await jobs_repo.update(
        session,
        job_id=job.id,
        status="running",
        started_at=datetime.now(UTC),
        attempts=job.attempts + 1,
    )
    assert running is not None  # the job was just fetched by this same id

    try:
        chunks_embedded = await index_source(
            session, embeddings, source_id=UUID(running.payload["source_id"])
        )
    except Exception as exc:  # noqa: BLE001 - any failure marks the job failed, never raises
        return await _mark_failed(session, job_id=running.id, error=str(exc))

    return await _mark_succeeded(session, job_id=running.id, chunks_embedded=chunks_embedded)


async def _mark_succeeded(
    session: AsyncSession, *, job_id: UUID, chunks_embedded: int
) -> JobRecord:
    result: dict[str, Any] = {"chunks_embedded": chunks_embedded}
    updated = await jobs_repo.update(
        session, job_id=job_id, status="succeeded", finished_at=datetime.now(UTC), result=result
    )
    assert updated is not None
    return updated


async def _mark_failed(session: AsyncSession, *, job_id: UUID, error: str) -> JobRecord:
    updated = await jobs_repo.update(
        session, job_id=job_id, status="failed", finished_at=datetime.now(UTC), error=error
    )
    assert updated is not None
    return updated
