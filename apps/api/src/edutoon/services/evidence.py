"""Evidence Engine - indexing (Phase B) and claim verification (Phase D).

The API routes over this service live in
``api/routers/claim_verifications.py`` (Phase E); the read-side helpers they
call (``list_claim_verifications``, ``get_claim_verification``,
``list_claim_citations``) are at the bottom of this module.

Indexing is jobs-backed (``kind='build_evidence'``): ``enqueue_indexing_job``
creates the row, and ``worker.dispatch`` calls ``run_build_evidence_job``
once the ``worker`` process (``edutoon/worker/``) claims it.

Claim verification (``verify_claim``) is synchronous, not jobs-backed: embed
the claim, retrieve the top candidates via Phase C's project-scoped
similarity search, ask the LLM to judge the claim against exactly those
candidates, and persist the verdict - citations only for chunks the LLM
actually named, never the full candidate set. A claim can never be
persisted as "supported" without at least one citation; see the guard in
``verify_claim`` below.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, JobAbandonedError, NotFoundError
from edutoon.core.pagination import DEFAULT_PAGE_SIZE, Page
from edutoon.domain.embedding_batches import EMBEDDING_DIMENSIONS, estimate_tokens, plan_batches
from edutoon.domain.llm_usage import UsageReport
from edutoon.providers.embeddings import Embeddings
from edutoon.providers.llm import LLM, LLMResponseError, Passage
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories.claim_verifications import (
    ClaimCitationRecord as ClaimCitationRecord,
)
from edutoon.repositories.claim_verifications import (
    ClaimVerificationRecord as ClaimVerificationRecord,
)
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.source_chunks import DEFAULT_SIMILARITY_LIMIT
from edutoon.services import spend_guard
from edutoon.services.job_errors import describe_job_failure

log = structlog.get_logger()

BUILD_EVIDENCE_JOB_KIND = "build_evidence"

STALE_INDEXING_ERROR = (
    "The worker stopped before finishing indexing this source, so it was marked as failed. "
    "Nothing already indexed is lost - retry to continue from where it stopped."
)


@dataclass(slots=True)
class IndexProgress:
    """What indexing has durably done so far. Filled in by :func:`index_source` as each batch is
    committed, so a job that fails part-way can say how far it got."""

    total: int = 0
    embedded: int = 0

    @property
    def remaining(self) -> int:
        return self.total - self.embedded


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
    session: AsyncSession,
    embeddings: Embeddings,
    *,
    source_id: UUID,
    progress: IndexProgress | None = None,
    on_batch: Callable[[int, int], Awaitable[None]] | None = None,
) -> int:
    """Embed every chunk of ``source_id`` that doesn't have one yet.

    Returns the number of chunks embedded. Idempotent by construction: a
    re-run (e.g. after a prior partial failure) only ever touches chunks
    still missing an embedding, never re-embeds ones that already have one,
    and a chunk's vector lives on the chunk itself - so a retry can never
    leave a second copy behind.

    The chunks are embedded in batches sized to the provider's limits
    (``domain.embedding_batches``), in document order, and **each batch is
    committed as soon as it is stored**. A batch that fails - after the
    provider's own transient-failure retries - therefore costs only itself: every
    earlier batch stays indexed, nothing of the failed batch is written, and running
    the job again continues from the first chunk still missing a vector. ``progress``
    (if given) is updated after each commit. ``on_batch(embedded, total)`` (if given) is awaited
    after each batch is stored and before it is committed - the job uses it to report progress,
    which is its heartbeat, in the very transaction that makes the batch durable.
    """
    chunks = await source_chunks_repo.list_missing_embedding_by_source(session, source_id)
    if progress is not None:
        progress.total = len(chunks)
    if not chunks:
        return 0

    embedded = 0
    batches = plan_batches([estimate_tokens(chunk.content) for chunk in chunks], embeddings.limits)
    for batch in batches:
        group = [chunks[i] for i in batch]
        vectors = await embeddings.embed([chunk.content for chunk in group])
        if len(vectors) != len(group) or any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            # Refused before anything is stored: a vector of the wrong width cannot be
            # compared with the others, and the database would reject it anyway.
            raise ValueError(
                f"The embedding model returned vectors that are not {EMBEDDING_DIMENSIONS} wide."
            )
        try:
            for chunk, vector in zip(group, vectors, strict=True):
                await source_chunks_repo.update_embedding(
                    session, chunk_id=chunk.id, embedding=vector, embedding_model=embeddings.model
                )
            if on_batch is not None:
                await on_batch(embedded + len(group), len(chunks))
            await session.commit()  # this batch is durable whatever happens to the next
        except Exception:
            # Nothing of a batch that failed part-way is kept: not a half-stored batch, and not
            # one a worker that had been replaced (JobAbandonedError) was still writing.
            await session.rollback()
            raise
        embedded += len(group)
        if progress is not None:
            progress.embedded = embedded
    return embedded


async def run_build_evidence_job(
    session: AsyncSession, embeddings: Embeddings, *, job_id: UUID
) -> JobRecord:
    """Process one queued ``build_evidence`` job end to end: marks it
    running, indexes the source named in its payload, marks it
    succeeded (with the chunk count) or failed (with the error message).

    This is the function ``worker.dispatch`` calls immediately after the
    worker claims the job (``edutoon/worker/``).
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
    # Make "running" visible (and release the row's lock) before the long work: progress is
    # reported, and a crashed worker recovered, through the row from here on.
    await session.commit()

    progress = IndexProgress()

    async def _report(embedded: int, total: int) -> None:
        """Progress is the heartbeat (a live job is never reclaimed as stale) and the fence (a job
        that was reclaimed anyway finds out here and stops)."""
        stamp = {
            "stage": "embedding",
            "chunks_embedded": embedded,
            "chunks_total": total,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if not await jobs_repo.record_progress(
            session, job_id=running.id, payload={**running.payload, "progress": stamp}
        ):
            raise JobAbandonedError

    try:
        chunks_embedded = await index_source(
            session,
            embeddings,
            source_id=UUID(running.payload["source_id"]),
            progress=progress,
            on_batch=_report,
        )
    except Exception as exc:  # noqa: BLE001 - any failure marks the job failed, never raises
        error = describe_job_failure(exc, job_id=running.id, kind=BUILD_EVIDENCE_JOB_KIND)
        log.warning(
            "evidence.indexing_failed",
            job_id=str(running.id),
            chunks_embedded=progress.embedded,
            chunks_remaining=progress.remaining,
        )
        return await _mark_failed(
            session,
            job_id=running.id,
            error=error,
            result={
                "chunks_embedded": progress.embedded,
                "chunks_remaining": progress.remaining,
            },
        )

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


async def _mark_failed(
    session: AsyncSession,
    *,
    job_id: UUID,
    error: str,
    result: dict[str, Any] | None = None,
) -> JobRecord:
    fields: dict[str, Any] = {
        "status": "failed",
        "finished_at": datetime.now(UTC),
        "error": error,
    }
    if result is not None:
        fields["result"] = result
    updated = await jobs_repo.update(session, job_id=job_id, **fields)
    assert updated is not None
    return updated


async def retry_indexing_job(
    session: AsyncSession, *, project_id: UUID, source_id: UUID
) -> JobRecord:
    """Put a source's *failed* indexing back in the queue.

    The job keeps its identity (its idempotency key is the source id), so the same source
    still can only ever have one indexing job - a retry re-runs that job, it does not create a
    second. Because :func:`index_source` only embeds chunks that still lack a vector, the retry
    does exactly the work that was left.

    Raises :class:`~edutoon.core.errors.NotFoundError` if the source was never queued for
    indexing, and :class:`~edutoon.core.errors.ConflictError` if the job is still queued or
    running, or has already succeeded - there is nothing to retry. The caller checks the source
    belongs to the project (rule 9) first.
    """
    # An indexing job a crashed worker left "running" would otherwise block its own retry for
    # ever: fail it first if it has shown no sign of life (its last batch's progress) for too long.
    for stale in await jobs_repo.fail_stale_running(
        session,
        project_id=project_id,
        kind=BUILD_EVIDENCE_JOB_KIND,
        stale_after=timedelta(seconds=get_settings().INDEXING_STALE_AFTER_SECONDS),
        error=STALE_INDEXING_ERROR,
    ):
        log.warning("evidence.stale_indexing_recovered", job_id=str(stale.id))

    job = await jobs_repo.get_by_idempotency_key(
        session,
        project_id=project_id,
        kind=BUILD_EVIDENCE_JOB_KIND,
        idempotency_key=str(source_id),
    )
    if job is None:
        raise NotFoundError(f"Uploaded source {source_id} has no indexing job to retry.")
    if job.status in ("queued", "running"):
        raise ConflictError("This source is already being indexed; wait for it to finish.")
    if job.status == "succeeded":
        raise ConflictError("This source is already indexed.")
    requeued = await jobs_repo.requeue_failed(session, job_id=job.id)
    if requeued is None:  # it changed state between the read and the update
        raise ConflictError("This source's indexing changed state; try again.")
    return requeued


async def verify_claim(
    session: AsyncSession,
    embeddings: Embeddings,
    llm: LLM,
    *,
    project_id: UUID,
    claim_text: str,
    topic_id: UUID | None = None,
    limit: int = DEFAULT_SIMILARITY_LIMIT,
    usage: UsageReport | None = None,
) -> ClaimVerificationRecord:
    """Verify ``claim_text`` against ``project_id``'s indexed source material.

    Flow: embed the claim -> retrieve the top ``limit`` candidates via
    Phase C's project-scoped similarity search -> ask the LLM to judge the
    claim against exactly those candidates -> persist the verdict, with
    citations only for the candidates the LLM actually named.

    The core guarantee: a claim is never persisted as "supported" without
    at least one citation. If the LLM reports "supported" but names no
    passage (a hallucinated-confidence failure mode - the model being
    confident without pointing at anything real), the stored status is
    downgraded to "unsupported" here. This is enforced at the point of
    persistence, not merely assumed from the model's own self-report.

    ``topic_id`` is ``None`` for a claim typed in by hand through the manual
    ``POST /claim-verifications`` route (unchanged); ``services.claim_extraction``
    (Phase 6) passes the topic a candidate was drawn from, so the resulting
    verdict is traceable back to it. It changes nothing about how the claim is
    verified - retrieval and judging are exactly the same lookup either way,
    still project-scoped, never narrowed to the topic's own material: a
    keyword-only topic with nothing indexed genuinely has no material to
    verify against, and this is exactly the "no candidates" branch below, not
    a bypass of it.

    ``usage`` (if given) receives the judge call's token usage as ``claim_judge`` - a job that
    verifies many claims passes its report so the cost of judging is part of its result. A claim
    typed in by hand has no job; its usage is in the ``llm.call`` log line only.
    """
    [claim_vector] = await embeddings.embed([claim_text])
    candidates = await source_chunks_repo.search_by_similarity(
        session, project_id=project_id, query_embedding=claim_vector, limit=limit
    )

    if not candidates:
        return await claim_verifications_repo.create(
            session,
            project_id=project_id,
            claim_text=claim_text,
            status="unsupported",
            embedding_model=embeddings.model,
            llm_model=llm.model,
            rationale="No indexed source material exists for this project yet.",
            topic_id=topic_id,
        )

    candidates_by_id = {str(chunk.id): (chunk, similarity) for chunk, similarity in candidates}
    passages = [
        Passage(id=chunk_id, content=chunk.content)
        for chunk_id, (chunk, _similarity) in candidates_by_id.items()
    ]

    if usage is not None:
        usage.ensure_capacity()  # the job's call tripwire: stop before the paid call
    try:
        verdict = await llm.judge_claim(claim_text, passages)
    except LLMResponseError as exc:
        if usage is not None:
            usage.add("claim_judge", exc.usage)  # an answer that was paid for and rejected
        raise
    if usage is not None:
        usage.add("claim_judge", verdict.usage)

    # Defense in depth: providers.llm.LLM already filters cited ids against
    # what it was shown, but this is the actual persistence boundary, so it
    # re-checks against the real candidate set rather than trusting that
    # filtering happened correctly one layer away.
    cited = [candidates_by_id[pid] for pid in verdict.cited_passage_ids if pid in candidates_by_id]

    status = verdict.status
    if status == "supported" and not cited:
        status = "unsupported"

    citations = [
        NewClaimCitation(source_chunk_id=chunk.id, similarity=similarity)
        for chunk, similarity in cited
    ]

    return await claim_verifications_repo.create(
        session,
        project_id=project_id,
        claim_text=claim_text,
        status=status,
        embedding_model=embeddings.model,
        llm_model=llm.model,
        rationale=verdict.rationale,
        topic_id=topic_id,
        citations=citations,
    )


async def verify_manual_claim(
    session: AsyncSession,
    embeddings: Embeddings,
    llm: LLM,
    *,
    project_id: UUID,
    claim_text: str,
) -> ClaimVerificationRecord:
    """A claim typed in by hand (``POST /claim-verifications``): :func:`verify_claim` behind the
    spend guard. Each one is an embedding and a model call the caller can repeat as fast as they
    like, so a project may have only ``MAX_MANUAL_CLAIM_VERIFICATIONS_PER_PROJECT_PER_HOUR`` of
    them an hour (:mod:`edutoon.services.spend_guard`)."""
    await spend_guard.ensure_can_verify_manual_claim(session, project_id=project_id)
    return await verify_claim(
        session, embeddings, llm, project_id=project_id, claim_text=claim_text
    )


# --- read side (Phase E: API-facing retrieval of persisted verdicts) ----------------


async def list_claim_verifications(
    session: AsyncSession,
    *,
    project_id: UUID,
    topic_id: UUID | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> Page[ClaimVerificationRecord]:
    """A project's claim verifications, newest first (keyset-paginated).
    ``topic_id`` narrows the page to claims extracted for that one topic.

    Not ownership-scoped itself - callers check the project belongs to the
    caller first (rule 9), exactly as the ``sources`` routes do.
    """
    return await claim_verifications_repo.list_by_project(
        session, project_id, topic_id=topic_id, limit=limit, cursor=cursor
    )


async def get_claim_verification(
    session: AsyncSession, *, verification_id: UUID, project_id: UUID
) -> ClaimVerificationRecord:
    """Ownership-scoped fetch (rule 9): a verification belonging to a
    different project looks exactly like one that doesn't exist.
    """
    verification = await claim_verifications_repo.get_by_id_for_project(
        session, verification_id, project_id
    )
    if verification is None:
        raise NotFoundError(f"Claim verification {verification_id} not found.")
    return verification


async def list_claim_citations(
    session: AsyncSession, verification_id: UUID
) -> list[ClaimCitationRecord]:
    return await claim_verifications_repo.list_citations(session, verification_id)


async def list_claim_citations_for(
    session: AsyncSession, verification_ids: Sequence[UUID]
) -> dict[UUID, list[ClaimCitationRecord]]:
    """Citations for many verifications at once, grouped by verification id -
    lets the list endpoint attach citations without an N+1.
    """
    return await claim_verifications_repo.list_citations_for(session, verification_ids)
