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

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.core.pagination import DEFAULT_PAGE_SIZE, Page
from edutoon.providers.embeddings import Embeddings
from edutoon.providers.llm import LLM, Passage
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
from edutoon.services.job_errors import describe_job_failure

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

    try:
        chunks_embedded = await index_source(
            session, embeddings, source_id=UUID(running.payload["source_id"])
        )
    except Exception as exc:  # noqa: BLE001 - any failure marks the job failed, never raises
        error = describe_job_failure(exc, job_id=running.id, kind=BUILD_EVIDENCE_JOB_KIND)
        return await _mark_failed(session, job_id=running.id, error=error)

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


async def verify_claim(
    session: AsyncSession,
    embeddings: Embeddings,
    llm: LLM,
    *,
    project_id: UUID,
    claim_text: str,
    limit: int = DEFAULT_SIMILARITY_LIMIT,
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
        )

    candidates_by_id = {str(chunk.id): (chunk, similarity) for chunk, similarity in candidates}
    passages = [
        Passage(id=chunk_id, content=chunk.content)
        for chunk_id, (chunk, _similarity) in candidates_by_id.items()
    ]

    verdict = await llm.judge_claim(claim_text, passages)

    # Defense in depth: providers.llm.LLM already filters cited ids against
    # what it was shown, but this is the actual persistence boundary, so it
    # re-checks against the real candidate set rather than trusting that
    # filtering happened correctly one layer away.
    cited = [
        candidates_by_id[pid] for pid in verdict.cited_passage_ids if pid in candidates_by_id
    ]

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
        citations=citations,
    )


# --- read side (Phase E: API-facing retrieval of persisted verdicts) ----------------


async def list_claim_verifications(
    session: AsyncSession,
    *,
    project_id: UUID,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> Page[ClaimVerificationRecord]:
    """A project's claim verifications, newest first (keyset-paginated).

    Not ownership-scoped itself - callers check the project belongs to the
    caller first (rule 9), exactly as the ``sources`` routes do.
    """
    return await claim_verifications_repo.list_by_project(
        session, project_id, limit=limit, cursor=cursor
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
