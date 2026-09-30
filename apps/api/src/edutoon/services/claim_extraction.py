"""Candidate Claim Extraction - service layer (Phase 6).

Turns a project's *accepted* topics into candidate factual claims, then runs
each candidate through the existing Evidence Engine
(:func:`edutoon.services.evidence.verify_claim`, :meth:`LLM.judge_claim` -
both unchanged) exactly as a claim typed in by hand already is. A topic's
``summary`` and ``keywords`` are never themselves treated as verified: only a
claim that has been through :func:`~edutoon.services.evidence.verify_claim`
carries a status and citations, and script generation already only ever reads
``status='supported'`` claims (``claim_verifications_repo.list_supported_by_project``,
unchanged).

Mirrors ``services/topic_extraction.py`` deliberately - one job pattern, not a
second one - with one structural difference the two domains force: a topic
outline is a single tree that must be replaced as a whole, so a re-run there
is all-or-nothing per project. A project's accepted topics are independent of
each other, and ``claim_verifications`` is an append-only ledger that is never
deleted (a script may already cite one, ``ON DELETE RESTRICT``), so a claim
extraction run processes each topic independently and never deletes anything:

- ``enqueue_claim_extraction`` - the caller-facing entry point. Verifies
  project ownership, replays an earlier request with the same idempotency
  key, refuses a second in-flight extraction, checks there is at least one
  accepted topic, creates an ``extract_claims`` job and writes an audit-log
  row.
- ``run_extract_claims_job`` - what the ``worker`` process calls after
  claiming the job. Processes each accepted topic in turn:

  1. Skip it (by default, unless ``replace``) if it already has extracted
     claims (``claim_verifications.topic_id`` set) - the incremental,
     idempotent case: accepting a few more topics and re-running only ever
     does new work for the new ones.
  2. Gather the topic's own cited passages, if it has any (a keyword-only
     topic with nothing indexed has none - see below).
  3. One model call (:meth:`LLM.extract_claims`) proposes candidate claim
     text for this topic alone.
  4. :func:`edutoon.domain.claim_extraction.validate_candidates` cleans and
     de-duplicates the candidates against this topic's own already-extracted
     claim texts, so a replace/re-run never re-verifies (and re-records) the
     same wording.
  5. Each surviving candidate is independently verified
     (:func:`~edutoon.services.evidence.verify_claim`, unmodified) and
     persisted immediately.

  One topic's failure - a malformed model response, a database error - never
  destroys another topic's successful claims: each topic's writes are one
  SAVEPOINT, and the job keeps going. The job itself only ends ``failed`` if
  every eligible topic failed and nothing was verified; otherwise it
  ``succeeded``, and its result lists any per-topic failures alongside what
  it did verify, so a partial run is never mistaken for a clean one.

**Keyword-only projects.** A topic with no uploaded PDF behind it has no
indexed source material, so :func:`~edutoon.services.evidence.verify_claim`'s
retrieval finds no candidates and every claim from it is correctly persisted
``unsupported`` with an explicit rationale - not a bypass of verification, and
not a new external knowledge source invented to work around it (there isn't
one): the core promise (rule: no unsupported claim ever reaches a script)
holds exactly as it does for a hand-typed claim against an unindexed project.
This is expected, documented behaviour, not an error - candidates are still
worth extracting and recording so the gap is visible to the project's owner
once a PDF is added and the topic's claims are re-run with ``replace``.

Architecture (CLAUDE.md rules 1, 2, 3, 9): this module is the only place
(besides ``topic_extraction`` and ``evidence``) allowed to touch
``providers.llm`` / ``providers.embeddings`` for this pipeline; it never
issues SQL itself; it builds ``ClaimCandidateLimits`` from settings and passes
them explicitly, so the domain never reads configuration; every path is
project + owner scoped and a project the caller does not own is a 404.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import Settings, get_settings
from edutoon.core.errors import ConflictError, JobError, NotFoundError, ValidationError
from edutoon.domain.claim_extraction import ClaimCandidateLimits, validate_candidates
from edutoon.domain.llm_usage import UsageReport
from edutoon.domain.topic import SourcePassage
from edutoon.domain.topic_tree import accepted_topics, select_passages
from edutoon.providers.embeddings import Embeddings
from edutoon.providers.llm import LLM, ClaimTopicBrief
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import topic_citations as topic_citations_repo
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.project_topics import ProjectTopicRecord
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import evidence as evidence_service
from edutoon.services import projects as projects_service
from edutoon.services.job_errors import describe_job_failure
from edutoon.services.llm_usage import record_call

log = structlog.get_logger()

EXTRACT_CLAIMS_JOB_KIND = "extract_claims"

AUDIT_ENTITY_TYPE = "claim_extraction"
AUDIT_ACTION_ENQUEUED = "claims.extraction_enqueued"
AUDIT_ACTION_RECOVERED = "claims.extraction_recovered"

STALE_EXTRACTION_ERROR = (
    "The worker stopped before finishing this claim extraction, so it was marked as "
    "failed. Any topic already processed keeps its verified claims - run the "
    "extraction again to cover the rest."
)


@dataclass(frozen=True, slots=True)
class ClaimExtractionEnqueued:
    job: JobRecord
    replayed: bool


def claim_limits_from_settings(settings: Settings) -> ClaimCandidateLimits:
    """Build the domain's :class:`ClaimCandidateLimits` from application
    settings - the one place configuration crosses into the domain, mirroring
    ``topic_extraction.topic_limits_from_settings``.
    """
    return ClaimCandidateLimits(
        max_claims_per_topic=settings.MAX_CLAIMS_PER_TOPIC,
        max_claim_text_chars=settings.MAX_CLAIM_TEXT_CHARS,
    )


# --- enqueue -----------------------------------------------------------------------


async def enqueue_claim_extraction(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_id: UUID,
    idempotency_key: str,
    replace: bool = False,
) -> ClaimExtractionEnqueued:
    """Queue a claim extraction for ``project_id``.

    In order:

    1. Raises :class:`~edutoon.core.errors.NotFoundError` if the project isn't
       owned by ``owner_id`` (rule 9).
    2. A repeat call with the same ``idempotency_key`` returns the original
       job with ``replayed=True``.
    3. Raises :class:`~edutoon.core.errors.ConflictError` if another claim
       extraction is already ``queued`` or ``running`` for the project.
    4. Raises :class:`~edutoon.core.errors.ValidationError` if the project has
       no accepted topic to extract claims from.

    Unlike ``topic_extraction.enqueue_topic_extraction`` there is no
    "already extracted, pass replace to overwrite" gate: extraction is
    per-topic and additive, so accepting more topics later and enqueueing
    again is the ordinary, idempotent way to cover them - the job itself
    decides, topic by topic, what is already covered.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    existing_job = await jobs_repo.get_by_idempotency_key(
        session,
        project_id=project_id,
        kind=EXTRACT_CLAIMS_JOB_KIND,
        idempotency_key=idempotency_key,
    )
    if existing_job is not None:
        return ClaimExtractionEnqueued(job=existing_job, replayed=True)

    await refuse_if_extraction_active(session, project_id=project_id)

    topics = await project_topics_repo.list_all_by_project(session, project_id)
    if not accepted_topics(topics):
        raise ValidationError(
            "There are no accepted topics to extract claims from. Accept at least one "
            "topic first."
        )

    job = await jobs_repo.create(
        session,
        project_id=project_id,
        kind=EXTRACT_CLAIMS_JOB_KIND,
        payload={"project_id": str(project_id), "replace": replace},
        idempotency_key=idempotency_key,
    )
    await audit_logs_service.record_audit_log(
        session,
        action=AUDIT_ACTION_ENQUEUED,
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=job.id,
        project_id=project_id,
        actor_id=owner_id,
        context={"job_id": str(job.id), "replace": replace},
    )
    return ClaimExtractionEnqueued(job=job, replayed=False)


async def refuse_if_extraction_active(session: AsyncSession, *, project_id: UUID) -> None:
    """Take the project's claim-extraction lock, then raise
    :class:`~edutoon.core.errors.ConflictError` if an extraction is ``queued``
    or ``running``. Mirrors ``topic_extraction.refuse_if_extraction_active``
    exactly, scoped to :data:`EXTRACT_CLAIMS_JOB_KIND` - independent of any
    topic extraction the project has in flight.
    """
    await jobs_repo.lock_project_kind(session, project_id=project_id, kind=EXTRACT_CLAIMS_JOB_KIND)
    await _recover_stale_extractions(session, project_id=project_id)
    active = await jobs_repo.get_active_by_project_kind(
        session, project_id=project_id, kind=EXTRACT_CLAIMS_JOB_KIND
    )
    if active is not None:
        raise ConflictError(
            "Claim extraction is already queued or running for this project; wait for "
            "it to finish."
        )


async def _recover_stale_extractions(session: AsyncSession, *, project_id: UUID) -> None:
    """Fail any claim extraction a crashed worker left ``running``. Mirrors
    ``topic_extraction._recover_stale_extractions``.
    """
    stale_after = get_settings().CLAIM_EXTRACTION_STALE_AFTER_SECONDS
    recovered = await jobs_repo.fail_stale_running(
        session,
        project_id=project_id,
        kind=EXTRACT_CLAIMS_JOB_KIND,
        stale_after=timedelta(seconds=stale_after),
        error=STALE_EXTRACTION_ERROR,
    )
    for job in recovered:
        log.warning(
            "claims.stale_extraction_recovered",
            job_id=str(job.id),
            project_id=str(project_id),
            started_at=job.started_at.isoformat() if job.started_at else None,
            stale_after_seconds=stale_after,
        )
        await audit_logs_service.record_audit_log(
            session,
            action=AUDIT_ACTION_RECOVERED,
            entity_type=AUDIT_ENTITY_TYPE,
            entity_id=job.id,
            project_id=project_id,
            actor_type="system",
            context={"job_id": str(job.id), "stale_after_seconds": stale_after},
        )


# --- run (background job) ----------------------------------------------------------------


async def run_extract_claims_job(
    session: AsyncSession, embeddings: Embeddings, llm: LLM, *, job_id: UUID
) -> JobRecord:
    """Process one queued ``extract_claims`` job end to end.

    Marks it running, extracts and verifies claims for each eligible accepted
    topic (see the module docstring), then marks it ``succeeded`` with the
    result summary - or ``failed`` with the error, only when every eligible
    topic failed and nothing was verified. Only :class:`NotFoundError` for a
    ``job_id`` that does not exist at all (a caller bug) is ever raised,
    matching ``topic_extraction.run_extract_topics_job``.
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
    assert running is not None  # just fetched by this id

    report = UsageReport()
    try:
        project_id, replace = _parse_payload(running.payload)
        result = await _run(
            session,
            embeddings,
            llm,
            project_id=project_id,
            replace=replace,
            job_id=running.id,
            report=report,
        )
    except Exception as exc:  # noqa: BLE001 - any failure fails the job, never raises
        error = describe_job_failure(exc, job_id=running.id, kind=EXTRACT_CLAIMS_JOB_KIND)
        return await _mark_job_failed(
            session, job_id=running.id, error=error, result={"usage": report.as_dict()}
        )

    return await _mark_job_succeeded(session, job_id=running.id, result=result)


@dataclass(frozen=True, slots=True)
class _TopicOutcome:
    """One eligible topic's result, kept only for :func:`_result` to
    summarise - never persisted itself.
    """

    topic_id: UUID
    candidates_generated: int
    verified_statuses: list[str]


@dataclass(frozen=True, slots=True)
class _TopicFailure:
    topic_id: UUID
    topic_title: str
    error: str


def _parse_payload(payload: dict[str, Any]) -> tuple[UUID, bool]:
    try:
        project_id = UUID(str(payload["project_id"]))
    except (KeyError, ValueError) as exc:
        raise JobError("The extraction job has no valid project_id in its payload.") from exc
    return project_id, bool(payload.get("replace", False))


async def _run(
    session: AsyncSession,
    embeddings: Embeddings,
    llm: LLM,
    *,
    project_id: UUID,
    replace: bool,
    job_id: UUID,
    report: UsageReport,
) -> dict[str, object]:
    settings = get_settings()
    limits = claim_limits_from_settings(settings)

    project = await projects_repo.get_by_id(session, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")

    topics = await project_topics_repo.list_all_by_project(session, project_id)
    eligible = accepted_topics(topics)
    if not eligible:
        raise ValidationError(
            "There are no accepted topics to extract claims from. Accept at least one "
            "topic first."
        )

    processed: list[_TopicOutcome] = []
    skipped: list[UUID] = []
    failures: list[_TopicFailure] = []

    for topic in eligible:
        existing_texts = await claim_verifications_repo.list_claim_texts_by_topic(
            session, topic.id
        )
        if existing_texts and not replace:
            skipped.append(topic.id)
            continue

        try:
            passages = await _passages_for_topic(
                session,
                project_id=project_id,
                topic=topic,
                budget_chars=settings.CLAIM_INPUT_BUDGET_CHARS,
            )
            brief = ClaimTopicBrief(
                title=topic.title,
                language=project.language,
                summary=topic.summary,
                keywords=tuple(topic.keywords),
            )
            draft = await record_call(
                report, "claim_extraction", llm.extract_claims(brief, passages, limits=limits)
            )

            candidate_texts = validate_candidates(
                draft, limits=limits, existing_claim_texts=existing_texts
            )

            statuses: list[str] = []
            async with session.begin_nested():
                for text in candidate_texts:
                    verification = await evidence_service.verify_claim(
                        session,
                        embeddings,
                        llm,
                        project_id=project_id,
                        claim_text=text,
                        topic_id=topic.id,
                        usage=report,
                    )
                    statuses.append(verification.status)
        except Exception as exc:  # noqa: BLE001 - one topic's failure must not sink the job
            failures.append(
                _TopicFailure(
                    topic_id=topic.id,
                    topic_title=topic.title,
                    error=describe_job_failure(exc, job_id=job_id, kind=EXTRACT_CLAIMS_JOB_KIND),
                )
            )
            continue

        processed.append(
            _TopicOutcome(
                topic_id=topic.id,
                candidates_generated=len(candidate_texts),
                verified_statuses=statuses,
            )
        )

    if eligible and not processed and not skipped and failures:
        raise JobError(
            f"Claim extraction failed for every eligible topic ({len(failures)} of "
            f"{len(eligible)}); first error: {failures[0].error}"
        )

    return _result(
        eligible=eligible,
        processed=processed,
        skipped=skipped,
        failures=failures,
        replace=replace,
        report=report,
        embeddings_model=embeddings.model,
        llm_model=llm.model,
    )


async def _passages_for_topic(
    session: AsyncSession,
    *,
    project_id: UUID,
    topic: ProjectTopicRecord,
    budget_chars: int,
) -> list[SourcePassage]:
    """The topic's own cited material, budgeted the same way topic extraction
    budgets a whole document (:func:`edutoon.domain.topic_tree.select_passages`),
    just at a per-topic scale. Empty for a topic with no citations - a
    keyword-only topic, or one the material never covered - in which case
    :meth:`LLM.extract_claims` is called with no passages at all.
    """
    evidence_by_topic = await topic_citations_repo.list_evidence_for_topics(
        session, project_id=project_id, topic_ids=[topic.id]
    )
    evidence = evidence_by_topic.get(topic.id, [])
    if not evidence:
        return []

    chunks = await source_chunks_repo.list_by_ids(
        session, project_id=project_id, chunk_ids=[item.source_chunk_id for item in evidence]
    )
    chunks_by_id = {chunk.id: chunk for chunk in chunks}
    ordered = [
        SourcePassage(
            chunk_id=str(chunk.id),
            text=chunk.content,
            page_from=chunk.page_from,
            page_to=chunk.page_to,
        )
        for item in evidence
        if (chunk := chunks_by_id.get(item.source_chunk_id)) is not None
    ]
    return list(select_passages(ordered, budget_chars=budget_chars).passages)


def _result(
    *,
    eligible: list[ProjectTopicRecord],
    processed: list[_TopicOutcome],
    skipped: list[UUID],
    failures: list[_TopicFailure],
    replace: bool,
    report: UsageReport,
    embeddings_model: str,
    llm_model: str,
) -> dict[str, object]:
    """The structured job result: what ran, what it found, and - unlike a
    topic extraction, which is all-or-nothing - which topics were skipped
    (already covered) or failed (so a partial run is never mistaken for a
    clean one).
    """
    claims_by_status: dict[str, int] = {}
    for outcome in processed:
        for status in outcome.verified_statuses:
            claims_by_status[status] = claims_by_status.get(status, 0) + 1

    return {
        "topics_eligible": len(eligible),
        "topics_processed": len(processed),
        "topics_skipped_already_extracted": len(skipped),
        "topics_failed": len(failures),
        "candidates_generated": sum(outcome.candidates_generated for outcome in processed),
        "claims_verified": sum(len(outcome.verified_statuses) for outcome in processed),
        "claims_by_status": claims_by_status,
        "failures": [
            {"topic_id": str(f.topic_id), "topic_title": f.topic_title, "error": f.error}
            for f in failures
        ],
        "replace": replace,
        "input_tokens": report.total.input_tokens or 0,
        "output_tokens": report.total.output_tokens or 0,
        "usage": report.as_dict(),
        "embedding_model": embeddings_model,
        "llm_model": llm_model,
    }


async def _mark_job_succeeded(
    session: AsyncSession, *, job_id: UUID, result: dict[str, object]
) -> JobRecord:
    updated = await jobs_repo.update(
        session, job_id=job_id, status="succeeded", finished_at=datetime.now(UTC), result=result
    )
    assert updated is not None
    return updated


async def _mark_job_failed(
    session: AsyncSession, *, job_id: UUID, error: str, result: dict[str, object] | None = None
) -> JobRecord:
    updated = await jobs_repo.update(
        session,
        job_id=job_id,
        status="failed",
        finished_at=datetime.now(UTC),
        error=error,
        **({"result": result} if result is not None else {}),
    )
    assert updated is not None
    return updated
