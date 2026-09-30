"""Topic Extraction - service layer (Phase 5, Step 4).

Turns a project's material and/or the user's own topics into a persisted topic
outline. Mirrors ``services/scripts.py`` deliberately - one job pattern, not a
second one:

- ``enqueue_topic_extraction`` - the caller-facing entry point. Verifies
  project ownership, replays an earlier request with the same idempotency key,
  refuses a second in-flight extraction, checks there is something to extract
  from, applies the ``replace`` rules, creates an ``extract_topics`` job and
  writes an audit-log row.
- ``run_extract_topics_job`` - what the ``worker`` process
  (``edutoon/worker/``) calls after claiming the job. Any failure marks the job
  ``failed`` and never raises; the only exception is ``NotFoundError`` for a
  ``job_id`` that does not exist at all (a caller bug).

The flow a job runs::

    inputs (source chunks, user topics)
      -> mode (pdf | keywords | pdf_keywords)
      -> select_passages          (deterministic, whole-document, budgeted)
      -> LLM.extract_topics       (Step 3: one forced tool call)
      -> validate_and_build       (Step 2: evidence, hierarchy, origins, seeds)
      -> persist                  (one savepoint)
      -> structured job result

**The invariant that matters most: a failed extraction never destroys the last
successful topic set.** Everything that can fail - the reads, the model call,
provider validation, domain validation - happens *before* any write. The
writes (clear evidence, delete derived topics if replacing, update seeds,
insert the new tree and its citations) then run inside a single SAVEPOINT, so
if any of them fails the previous derived topics and their evidence are exactly
as they were.

Architecture (CLAUDE.md rules 1, 2, 3, 9): this module is the only topic layer
allowed to touch ``providers.llm``; it never issues SQL itself; it builds
``TopicLimits`` from settings and passes them explicitly, so the domain never
reads configuration; every path is project + owner scoped and a project the
caller does not own is a 404.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import Settings, get_settings
from edutoon.core.errors import ConflictError, JobError, NotFoundError, ValidationError
from edutoon.domain.llm_usage import UsageReport
from edutoon.domain.topic import (
    PassageSelection,
    SourcePassage,
    TopicLimits,
    TopicSeed,
    ValidatedTopicNode,
    ValidatedTopicTree,
)
from edutoon.domain.topic_tree import (
    NoTopicInputError,
    evidence_count_is_valid,
    resolve_input_mode,
    select_passages,
    validate_and_build,
)
from edutoon.providers.llm import LLM, TopicBrief
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import topic_citations as topic_citations_repo
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.project_topics import NewProjectTopic, ProjectTopicRecord
from edutoon.repositories.projects import ProjectRecord
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import projects as projects_service
from edutoon.services import spend_guard
from edutoon.services.job_errors import describe_job_failure
from edutoon.services.llm_usage import new_usage_report, record_call

log = structlog.get_logger()

EXTRACT_TOPICS_JOB_KIND = "extract_topics"

AUDIT_ENTITY_TYPE = "topic_extraction"
AUDIT_ACTION_ENQUEUED = "topics.extraction_enqueued"
AUDIT_ACTION_RECOVERED = "topics.extraction_recovered"

# What the owner sees on a job a crashed worker abandoned. Accurate because an
# extraction's writes are one transaction: a worker that died mid-run left
# nothing behind.
STALE_EXTRACTION_ERROR = (
    "The worker stopped before finishing this extraction, so it was marked as failed. "
    "Nothing was changed - run the extraction again."
)

_USER_ORIGIN = project_topics_repo.USER_ORIGIN
_REJECTED = "rejected"

# A project's title and description are free text with no stored length limit, and
# they reach the prompt *outside* the passage budget. Bounding what is sent keeps
# the input size - and so the cost of one extraction - bounded by settings and these
# constants whatever a project holds. It is prompt context only: nothing stored is
# shortened. (The language is one of a few supported tags, so it needs no bound.)
_BRIEF_SUBJECT_MAX_CHARS = 300
_BRIEF_DESCRIPTION_MAX_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class TopicExtractionEnqueued:
    job: JobRecord
    replayed: bool


def topic_limits_from_settings(settings: Settings) -> TopicLimits:
    """Build the domain's :class:`TopicLimits` from application settings.

    This is the one place configuration crosses into the domain: the result is
    handed to the provider and to ``validate_and_build`` explicitly, so neither
    ever reads settings or the environment.
    """
    return TopicLimits(
        max_topics=settings.MAX_TOPICS_PER_EXTRACTION,
        max_depth=settings.MAX_TOPIC_DEPTH,
        max_title_chars=settings.MAX_TOPIC_TITLE_CHARS,
        max_summary_chars=settings.MAX_TOPIC_SUMMARY_CHARS,
        max_keywords_per_topic=settings.MAX_TOPIC_KEYWORDS,
        max_keyword_chars=settings.MAX_TOPIC_KEYWORD_CHARS,
        max_evidence_per_topic=settings.MAX_TOPIC_EVIDENCE,
    )


def _derived(topics: Sequence[ProjectTopicRecord]) -> list[ProjectTopicRecord]:
    """Topics the pipeline made (``source`` / ``suggested``), as opposed to the
    ones the user typed.
    """
    return [topic for topic in topics if topic.origin != _USER_ORIGIN]


def _seed_topics(topics: Sequence[ProjectTopicRecord]) -> list[ProjectTopicRecord]:
    """The user topics an extraction works from: the user's own roots that they
    have not rejected. A rejected user topic is a decision to drop it, so it is
    neither offered to the model nor expanded.
    """
    return [
        topic
        for topic in topics
        if topic.origin == _USER_ORIGIN and topic.parent_id is None and topic.status != _REJECTED
    ]


# --- enqueue -----------------------------------------------------------------------


async def enqueue_topic_extraction(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_id: UUID,
    idempotency_key: str,
    replace: bool = False,
) -> TopicExtractionEnqueued:
    """Queue a topic extraction for ``project_id``.

    In order:

    1. Raises :class:`~edutoon.core.errors.NotFoundError` if the project isn't
       owned by ``owner_id`` (rule 9).
    2. A repeat call with the same ``idempotency_key`` returns the original job
       with ``replayed=True``.
    3. Raises :class:`~edutoon.core.errors.ConflictError` if another extraction
       is already ``queued`` or ``running`` for the project. The check is made
       under a per-project advisory lock (see ``jobs.lock_project_kind``), so
       two simultaneous requests cannot both pass it.
    4. Raises :class:`~edutoon.core.errors.ValidationError` if there is nothing
       to extract from (no parsed material and no user topics).
    5. ``replace=False`` raises ``ConflictError`` if topics were already
       extracted (nothing is overwritten unless asked); ``replace=True`` raises
       ``ConflictError`` if any script references one of the project's topics.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    existing_job = await jobs_repo.get_by_idempotency_key(
        session,
        project_id=project_id,
        kind=EXTRACT_TOPICS_JOB_KIND,
        idempotency_key=idempotency_key,
    )
    if existing_job is not None:
        return TopicExtractionEnqueued(job=existing_job, replayed=True)

    await refuse_if_extraction_active(session, project_id=project_id)

    topics = await project_topics_repo.list_all_by_project(session, project_id)
    chunk_count = await source_chunks_repo.count_by_project(session, project_id)
    try:
        mode = resolve_input_mode(
            has_passages=chunk_count > 0, has_seeds=bool(_seed_topics(topics))
        )
    except NoTopicInputError as exc:
        raise ValidationError(str(exc)) from exc

    await _check_replace_rules(session, project_id, topics=topics, replace=replace)
    await spend_guard.ensure_can_start_provider_job(session, project_id=project_id)

    job = await jobs_repo.create(
        session,
        project_id=project_id,
        kind=EXTRACT_TOPICS_JOB_KIND,
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
        context={"job_id": str(job.id), "replace": replace, "mode": mode},
    )
    return TopicExtractionEnqueued(job=job, replayed=False)


async def refuse_if_extraction_active(session: AsyncSession, *, project_id: UUID) -> None:
    """Take the project's extraction lock, then raise
    :class:`~edutoon.core.errors.ConflictError` if an extraction is ``queued``
    or ``running``.

    The lock (``jobs.lock_project_kind``) is held to the end of the caller's
    transaction, so whatever the caller goes on to write cannot interleave with
    an extraction being enqueued - and an extraction that is already running is
    refused here, rather than racing the caller's write. Used by
    :func:`enqueue_topic_extraction` itself and by every topic edit that an
    in-flight extraction would otherwise collide with.

    Before checking, an extraction that a crashed worker abandoned is failed
    (:func:`_recover_stale_extractions`), so a dead job cannot refuse these
    for ever; one a live worker is running is never affected.
    """
    await jobs_repo.lock_project_kind(session, project_id=project_id, kind=EXTRACT_TOPICS_JOB_KIND)
    await _recover_stale_extractions(session, project_id=project_id)
    active = await jobs_repo.get_active_by_project_kind(
        session, project_id=project_id, kind=EXTRACT_TOPICS_JOB_KIND
    )
    if active is not None:
        raise ConflictError(
            "Topic extraction is already queued or running for this project; wait for it to finish."
        )


async def _recover_stale_extractions(session: AsyncSession, *, project_id: UUID) -> None:
    """Fail any extraction a crashed worker left ``running``, so it cannot block
    the project for ever. Called under the project's extraction lock, so two
    requests cannot both recover the same job.

    A job that is genuinely being worked on is never touched: recovery needs it
    to be older than ``TOPIC_EXTRACTION_STALE_AFTER_SECONDS`` *and* not held by a
    live worker (see ``jobs.fail_stale_running``). What is recovered is logged
    and audited as a system action.
    """
    stale_after = get_settings().TOPIC_EXTRACTION_STALE_AFTER_SECONDS
    recovered = await jobs_repo.fail_stale_running(
        session,
        project_id=project_id,
        kind=EXTRACT_TOPICS_JOB_KIND,
        stale_after=timedelta(seconds=stale_after),
        error=STALE_EXTRACTION_ERROR,
    )
    for job in recovered:
        log.warning(
            "topics.stale_extraction_recovered",
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


async def _check_replace_rules(
    session: AsyncSession,
    project_id: UUID,
    *,
    topics: Sequence[ProjectTopicRecord],
    replace: bool,
) -> None:
    """The two rules about overwriting. Used when enqueueing and again when the
    job runs, because the project can change in between.
    """
    if replace:
        await _refuse_if_scripts_reference_topics(session, project_id)
    elif _derived(topics):
        raise ConflictError(
            "Topics have already been extracted for this project. Run the extraction "
            "again with replace to overwrite the derived topics (your own topics are kept)."
        )


async def _refuse_if_scripts_reference_topics(session: AsyncSession, project_id: UUID) -> None:
    """Deleting a topic a script points at would silently cut that link
    (``script_segments.topic_id`` is ``ON DELETE SET NULL``), so replacing is
    refused once any script does.
    """
    if await project_topics_repo.is_referenced_by_scripts(session, project_id):
        raise ConflictError(
            "A script already references this project's topics, so they cannot be "
            "replaced. Generate a new script from the current topics instead."
        )


# --- run (background job) ----------------------------------------------------------------


async def run_extract_topics_job(
    session: AsyncSession, llm: LLM, *, job_id: UUID
) -> JobRecord:
    """Process one queued ``extract_topics`` job end to end.

    Marks it running, extracts and persists (see the module docstring), then
    marks it ``succeeded`` with the result metadata - or ``failed`` with the
    error. Never raises for an extraction failure: a bad payload, a project that
    has vanished, a model or validation failure and a persistence failure all
    end as a failed job. Only :class:`NotFoundError` for a ``job_id`` that does
    not exist at all (a caller bug), matching ``scripts.run_generate_script_job``.
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

    report = new_usage_report()
    try:
        project_id, replace = _parse_payload(running.payload)
        result = await _extract_and_persist(
            session, llm, project_id=project_id, replace=replace, report=report
        )
    except Exception as exc:  # noqa: BLE001 - any failure fails the job, never raises
        error = describe_job_failure(exc, job_id=running.id, kind=EXTRACT_TOPICS_JOB_KIND)
        return await _mark_job_failed(
            session, job_id=running.id, error=error, result={"usage": report.as_dict()}
        )

    return await _mark_job_succeeded(session, job_id=running.id, result=result)


def _parse_payload(payload: Mapping[str, Any]) -> tuple[UUID, bool]:
    try:
        project_id = UUID(str(payload["project_id"]))
    except (KeyError, ValueError) as exc:
        raise JobError("The extraction job has no valid project_id in its payload.") from exc
    return project_id, bool(payload.get("replace", False))


async def _extract_and_persist(
    session: AsyncSession, llm: LLM, *, project_id: UUID, replace: bool, report: UsageReport
) -> dict[str, object]:
    settings = get_settings()
    limits = topic_limits_from_settings(settings)

    project = await projects_repo.get_by_id(session, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")

    # --- everything that can fail happens before the first write ---------------------
    topics = await project_topics_repo.list_all_by_project(session, project_id)
    await _check_replace_rules(session, project_id, topics=topics, replace=replace)

    chunks = await source_chunks_repo.list_by_project_in_order(session, project_id)
    seed_records = _seed_topics(topics)
    seeds = [
        TopicSeed(ref=str(topic.id), title=topic.title, position=topic.position)
        for topic in seed_records
    ]
    try:
        resolve_input_mode(has_passages=bool(chunks), has_seeds=bool(seeds))
    except NoTopicInputError as exc:
        raise ValidationError(str(exc)) from exc

    selection = select_passages(
        [
            SourcePassage(
                chunk_id=str(chunk.id),
                text=chunk.content,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
            )
            for chunk in chunks
        ],
        budget_chars=settings.TOPIC_INPUT_BUDGET_CHARS,
    )

    draft = await record_call(
        report,
        "topic_extraction",
        llm.extract_topics(_brief_for(project), selection.passages, seeds, limits=limits),
    )

    # Only the passages the model was actually shown may be cited - not every
    # chunk the project has.
    tree = validate_and_build(
        draft,
        seeds=seeds,
        known_chunk_ids={UUID(passage.chunk_id) for passage in selection.passages},
        limits=limits,
        first_root_position=_first_free_root_position(topics),
    )
    _assert_evidence_invariant(tree)
    if replace and _derived(topics) and all(node.origin == _USER_ORIGIN for node in tree.nodes):
        # Not a rule violation - a seeds-only outline is valid on its own - but
        # replacing a real derived set with nothing because the model returned
        # nothing useful would be data loss, so it is treated as a failure.
        raise JobError(
            "The new extraction produced no topics of its own, so the previous topics were kept."
        )

    # --- the writes: one savepoint, all or nothing -----------------------------------
    await _persist(
        session,
        project_id=project_id,
        tree=tree,
        replace=replace,
        seed_records={topic.id: topic for topic in seed_records},
    )

    return _result(tree=tree, selection=selection, replace=replace, report=report, model=llm.model)


def _clip(text: str, max_chars: int) -> str:
    """``text`` on one line and at most ``max_chars`` long, ending in an
    ellipsis if it was cut.
    """
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def _brief_for(project: ProjectRecord) -> TopicBrief:
    description = _clip(project.description or "", _BRIEF_DESCRIPTION_MAX_CHARS)
    return TopicBrief(
        subject=_clip(project.title, _BRIEF_SUBJECT_MAX_CHARS),
        description=description or None,
        language=project.language,
    )


def _first_free_root_position(topics: Sequence[ProjectTopicRecord]) -> int:
    """One past the highest root position among topics that will still exist
    after this run - every user root, rejected ones included, since a rejected
    topic still occupies its position.
    """
    return (
        max(
            (t.position for t in topics if t.parent_id is None and t.origin == _USER_ORIGIN),
            default=-1,
        )
        + 1
    )


def _assert_evidence_invariant(tree: ValidatedTopicTree) -> None:
    """Defence in depth at the persistence boundary: whatever the rules
    produced, an origin/evidence combination that breaks the invariant is never
    written. ``validate_and_build`` guarantees this; a future change to it
    must not be able to slip past silently.
    """
    for node in tree.nodes:
        if not evidence_count_is_valid(node.origin, len(node.evidence_chunk_ids)):
            raise ValueError(
                f"Topic {node.title!r} has origin {node.origin!r} with "
                f"{len(node.evidence_chunk_ids)} citations, which is not allowed."
            )


async def _persist(
    session: AsyncSession,
    *,
    project_id: UUID,
    tree: ValidatedTopicTree,
    replace: bool,
    seed_records: Mapping[UUID, ProjectTopicRecord],
) -> None:
    """Write ``tree`` inside one SAVEPOINT. Any failure rolls the whole thing
    back - including the deletion of the previous derived topics - and
    propagates, so the last successful topic set is left exactly as it was.
    """
    async with session.begin_nested():
        if replace:
            # Checked once more, right before the delete: the model call above can
            # take minutes, and a script may have been made from these topics in
            # the meantime. This narrows that window to the statements below; it
            # cannot close it without a lock shared with script creation (which
            # nothing yet links to topics).
            await _refuse_if_scripts_reference_topics(session, project_id)

        # Evidence is rebuilt wholesale from this run (it also covers user
        # topics, whose citations come from the latest extraction).
        await topic_citations_repo.delete_by_project(session, project_id)
        if replace:
            await project_topics_repo.delete_derived_by_project(session, project_id)

        topic_ids: dict[int, UUID] = {}
        for node in tree.nodes:
            if node.seed_ref is not None:
                topic_id = UUID(node.seed_ref)
                topic_ids[node.index] = topic_id
                await _fill_seed(session, project_id, topic_id, node, seed_records[topic_id])

        new_nodes = [node for node in tree.nodes if node.seed_ref is None]
        for depth in sorted({node.depth for node in new_nodes}):
            level = [node for node in new_nodes if node.depth == depth]
            rows = [
                NewProjectTopic(
                    project_id=project_id,
                    title=node.title,
                    parent_id=None if node.parent_index is None else topic_ids[node.parent_index],
                    summary=node.summary,
                    position=node.position,
                    depth=node.depth,
                    origin=node.origin,
                    keywords=node.keywords,
                )
                for node in level
            ]
            created = await project_topics_repo.create_many(session, rows)
            # RETURNING order is not guaranteed: map back by (parent, position).
            by_slot = {(topic.parent_id, topic.position): topic.id for topic in created}
            for node, row in zip(level, rows, strict=True):
                topic_ids[node.index] = by_slot[(row.parent_id, row.position)]

        for node in tree.nodes:
            if not node.evidence_chunk_ids:
                continue
            cited = await topic_citations_repo.add_many(
                session,
                project_id=project_id,
                topic_id=topic_ids[node.index],
                source_chunk_ids=node.evidence_chunk_ids,
            )
            if len(cited) != len(node.evidence_chunk_ids):
                raise ValueError(
                    f"Topic {node.title!r} cites material that does not belong to this project."
                )


async def _fill_seed(
    session: AsyncSession,
    project_id: UUID,
    topic_id: UUID,
    node: ValidatedTopicNode,
    current: ProjectTopicRecord,
) -> None:
    """Give a user topic the summary and keywords the model produced - but only
    where it has none of its own. The user's wording (title, and anything they
    wrote themselves) is authoritative and is never overwritten.
    """
    fields: dict[str, Any] = {}
    if node.summary and not current.summary:
        fields["summary"] = node.summary
    if node.keywords and not current.keywords:
        fields["keywords"] = list(node.keywords)
    if fields:
        await project_topics_repo.update_for_project(
            session, topic_id=topic_id, project_id=project_id, **fields
        )


def _result(
    *,
    tree: ValidatedTopicTree,
    selection: PassageSelection,
    replace: bool,
    report: UsageReport,
    model: str,
) -> dict[str, object]:
    """The structured job result. ``truncated`` says the model saw only part of
    the material (``chunks_used`` of ``chunks_total``), so a caller can report
    coverage honestly.
    """
    return {
        "mode": tree.mode,
        "topic_count": len(tree.nodes),
        "chunks_total": selection.chunks_total,
        "chunks_used": selection.chunks_used,
        "truncated": selection.truncated,
        "dropped_uncited": tree.dropped_uncited,
        "replaced": replace,
        "input_tokens": report.total.input_tokens or 0,
        "output_tokens": report.total.output_tokens or 0,
        "usage": report.as_dict(),
        "llm_model": model,
    }


async def _mark_job_succeeded(
    session: AsyncSession, *, job_id: UUID, result: dict[str, object]
) -> JobRecord:
    updated = await jobs_repo.update(
        session,
        job_id=job_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        result=result,
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
