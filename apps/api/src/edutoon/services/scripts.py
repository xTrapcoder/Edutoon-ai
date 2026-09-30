"""Script Generation Engine - service layer (Phase 7: duration-aware,
topic-scoped generation, replacing SG-4's single whole-project call).

- ``enqueue_script_generation`` - the caller-facing entry point. Verifies
  project ownership, loads the project's *accepted* topics and rejects
  immediately - before any provider call, before any job exists - if there
  are none, if there are more than ``MAX_SCRIPT_SECTIONS``, if the target
  duration cannot give each one the ``MIN_SECTION_DURATION_MS`` floor, or if
  any of them has zero *supported* claims of its own. Only once every one of
  those cheap, database-only checks passes does it create a fresh ``pending``
  script and a ``generate_script`` job, its payload freezing exactly which
  accepted topics this run covers. Idempotent on the caller's
  ``Idempotency-Key``: a repeat replays the original (script, job) instead of
  queuing a second run.
- ``run_generate_script_job`` - what the ``worker`` process
  (``edutoon/worker/``) calls after claiming the job. Marks the job
  ``running`` and commits immediately (releasing that row's lock before the
  potentially long generation work begins - see "Progress" below), then:
  plans a lesson outline for the frozen topic set, generates one section per
  topic (split into several ordered sub-sections when a topic's own budget is
  too long for one call - never one enormous call), applies at most one
  bounded duration-correction pass, validates the assembled whole, and
  attaches it to the pending script atomically. Any failure marks both the
  job and the script ``failed`` and leaves any previously ``approved`` script
  completely untouched; it never raises for a generation failure.
- ``assemble_script_detail`` - the read model: a script with its segments
  and, per segment, the verified claims it cites. Citations are batch-loaded
  (two queries total, regardless of segment count).
- ``approve_script`` - the ``draft -> approved`` transition, ownership
  checked, audit-logged.

**Duration.** ``project.target_duration_ms`` is split across accepted topics
by ``domain.script_duration.allocate_topic_budgets`` (a floor plus a share
proportional to each topic's own evidence volume) - the LLM is never asked to
decide durations. The WPM-based ``estimated_duration_ms`` this produces is a
target for a not-yet-real TTS estimate, not a hard contract: if it lands
outside ``SCRIPT_DURATION_TOLERANCE`` of the target, exactly one correction
pass re-generates every section with a rescaled word budget, and the result
is accepted afterwards regardless of the remaining difference. Only
structural and provenance failures reject a script outright.

**Topic scoping.** Each generated section may only cite its own topic's
supported claims (``claim_verifications_repo.list_supported_by_topic``) -
never another topic's, never a hand-typed claim (``topic_id IS NULL``, which
belongs to no topic's section). Every persisted segment's ``topic_id`` is set
by this module from the unit it came from, never taken from the model.

**Progress.** The worker normally runs a whole job inside one transaction,
committed only once the handler returns - which would make any interim
progress write block on (or be invisible until after) that same transaction.
This module instead commits its "running" transition immediately, keeps its
own session free of any write until the single final ``attach_draft`` call,
and reports interim progress (``preparing_outline``, ``generating_topic``,
``correcting_duration``, ``validating_provenance``, ``finalising``) through
short, independent sessions opened from the ``sessionmaker`` the worker
passes in - each one touching only the job's own ``payload``, never a
script/segment/claim-link row, so a progress write can never carry, and can
never block behind, partial script data. ``sessionmaker`` is optional so a
caller that only has a bare session (a direct unit test) simply gets no
interim progress, not an error.

**Atomicity.** A generation's segments live only in memory until
``repositories.scripts.attach_draft`` - one call, one SAVEPOINT - writes the
whole validated script and, in that same SAVEPOINT, supersedes the project's
previous ``draft`` / ``approved`` script (migration 0013: ``pending`` no
longer counts as "active", so the previous script is never touched before
this point). Nothing is ever attached in parts; a failure anywhere before
this call leaves the previous script exactly as it was.

Architecture (CLAUDE.md rules 1, 2, 3): this module is the only script layer
allowed to touch ``providers.llm``; it never issues SQL itself, going
through ``repositories`` for every read and write; every path is project +
owner scoped, and a resource the caller doesn't own is a 404, never a 403.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.core.config import Settings, get_settings
from edutoon.core.errors import (
    ConflictError,
    JobAbandonedError,
    JobError,
    NotFoundError,
    ValidationError,
)
from edutoon.core.pagination import DEFAULT_PAGE_SIZE, Page
from edutoon.domain.evidence_sufficiency import (
    EvidenceAssessment,
    assess_evidence,
    format_clock,
)
from edutoon.domain.generation_settings import MAX_TARGET_DURATION_MS
from edutoon.domain.llm_usage import UsageReport
from edutoon.domain.script import ScriptDraft, ScriptSegmentDraft
from edutoon.domain.script_duration import (
    allocate_topic_budgets,
    max_provider_calls,
    split_topic_budget,
)
from edutoon.domain.script_provenance import GroundingReference, filter_and_validate
from edutoon.domain.topic_tree import accepted_topics
from edutoon.providers.llm import (
    LLM,
    Claim,
    OutlineTopicBrief,
    ScriptBrief,
    SectionContinuity,
    SectionTopicBrief,
)
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories.claim_verifications import ClaimVerificationRecord
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.project_topics import ProjectTopicRecord
from edutoon.repositories.scripts import ScriptRecord, ScriptSegmentRecord
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import projects as projects_service
from edutoon.services import spend_guard
from edutoon.services.job_errors import describe_job_failure
from edutoon.services.llm_usage import new_usage_report, record_call

log = structlog.get_logger()

GENERATE_SCRIPT_JOB_KIND = "generate_script"

AUDIT_ENTITY_TYPE = "script"
AUDIT_ACTION_ENQUEUED = "script.generation_enqueued"
AUDIT_ACTION_APPROVED = "script.approved"
AUDIT_ACTION_RECOVERED = "script.generation_recovered"

STALE_GENERATION_ERROR = (
    "The worker stopped before finishing this script generation, so it was marked as "
    "failed. The previously approved script, if any, was never touched - write a new "
    "version to try again."
)


@dataclass(frozen=True, slots=True)
class ScriptGenerationEnqueued:
    script: ScriptRecord
    job: JobRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class ScriptSegmentDetail:
    segment: ScriptSegmentRecord
    claims: tuple[ClaimVerificationRecord, ...]


@dataclass(frozen=True, slots=True)
class ScriptDetail:
    script: ScriptRecord
    segments: tuple[ScriptSegmentDetail, ...]


# --- enqueue -----------------------------------------------------------------------


async def enqueue_script_generation(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_id: UUID,
    idempotency_key: str,
    llm_model: str | None = None,
) -> ScriptGenerationEnqueued:
    """Queue a script generation for ``project_id``.

    Raises :class:`~edutoon.core.errors.NotFoundError` if the project isn't
    owned by ``owner_id`` (rule 9). Raises
    :class:`~edutoon.core.errors.ValidationError`, before creating any job or
    making any provider call, if: there is no accepted topic; there are more
    accepted topics than ``MAX_SCRIPT_SECTIONS``; the target duration cannot
    give every accepted topic the ``MIN_SECTION_DURATION_MS`` floor; or any
    accepted topic has zero *supported* claims of its own. A repeat call with
    the same ``idempotency_key`` returns the original result with
    ``replayed=True``. It raises ``ValidationError`` when the project's target is longer than the
    longest video that can be rendered (``MAX_RENDER_DURATION_MS``), and when the verified
    evidence cannot carry
    the project's target duration (``domain.evidence_sufficiency``): a script is never padded.

    The job's payload freezes exactly which accepted topics this run covers
    (their ids, in reading order) - ``run_generate_script_job`` generates for
    this fixed set, not whatever happens to be accepted when it runs, so the
    duration and evidence guarantees just checked here still hold no matter
    when the worker gets to it.

    ``llm_model`` is the model the script is *queued for* (the API passes its
    script client's; ``SCRIPT_LLM_MODEL`` when omitted). The job that writes the
    script records the model it actually ran with, replacing this if they differ.
    """
    project = await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    existing_job = await jobs_repo.get_by_idempotency_key(
        session,
        project_id=project_id,
        kind=GENERATE_SCRIPT_JOB_KIND,
        idempotency_key=idempotency_key,
    )
    if existing_job is not None:
        script = await _script_from_job(session, existing_job)
        return ScriptGenerationEnqueued(script=script, job=existing_job, replayed=True)

    await _recover_stale_generations(session, project_id=project_id)

    settings = get_settings()

    # The first paid step is where an unsupported length is refused: a script that could not be
    # rendered would be written, narrated and storyboarded for nothing.
    if project.target_duration_ms > settings.MAX_RENDER_DURATION_MS:
        target = format_clock(project.target_duration_ms)
        ceiling = format_clock(settings.MAX_RENDER_DURATION_MS)
        raise ValidationError(
            f"This project's target of {target} is longer than the longest video that can be "
            f"made today ({ceiling}). Shorten the target in the project's settings, then write "
            "the script."
        )

    topics = await project_topics_repo.list_all_by_project(session, project_id)
    eligible = accepted_topics(topics)
    if not eligible:
        raise ValidationError(
            "This project has no accepted topics yet - accept at least one topic "
            "before generating a script."
        )
    if len(eligible) > settings.MAX_SCRIPT_SECTIONS:
        raise ValidationError(
            f"This project has {len(eligible)} accepted topics; a script may cover "
            f"at most {settings.MAX_SCRIPT_SECTIONS}. Reject some topics before "
            "generating a script."
        )
    if len(eligible) * settings.MIN_SECTION_DURATION_MS > project.target_duration_ms:
        raise ValidationError(
            f"{len(eligible)} accepted topics cannot each receive a section within "
            f"a {project.target_duration_ms}ms target duration. Accept fewer "
            "topics, or increase the target duration."
        )

    missing_evidence: list[str] = []
    supported_counts: list[int] = []
    for topic in eligible:
        supported = await claim_verifications_repo.list_supported_by_topic(
            session, project_id=project_id, topic_id=topic.id
        )
        supported_counts.append(len(supported))
        if not supported:
            missing_evidence.append(topic.title)
    if missing_evidence:
        names = ", ".join(f"'{title}'" for title in missing_evidence)
        raise ValidationError(
            f"These accepted topics have no verified evidence yet: {names}. Extract "
            "and verify claims for them, or reject them, before generating a script."
        )

    # Enough evidence for the length asked for? Refused here, from counts alone, before a job or
    # any paid call exists: a script stretched over too little evidence can only repeat itself or
    # invent facts (domain.evidence_sufficiency).
    assessment = assess_evidence(
        supported_counts,
        target_duration_ms=project.target_duration_ms,
        max_claims_per_topic=settings.MAX_CLAIMS_PER_SECTION,
        ms_per_claim=settings.EVIDENCE_SECONDS_PER_CLAIM * 1000,
    )
    if not assessment.is_sufficient:
        log.info(
            "scripts.evidence_insufficient",
            project_id=str(project_id),
            target_ms=assessment.target_duration_ms,
            defensible_ms=assessment.defensible_duration_ms,
            supported_claims=assessment.supported_claims,
            topics=assessment.topic_count,
        )
        raise ValidationError(_insufficient_evidence_message(assessment, settings))

    await spend_guard.ensure_can_start_provider_job(session, project_id=project_id)

    model = llm_model or settings.SCRIPT_LLM_MODEL

    script = await scripts_repo.create(session, project_id=project_id, llm_model=model)
    job = await jobs_repo.create(
        session,
        project_id=project_id,
        kind=GENERATE_SCRIPT_JOB_KIND,
        payload={
            "script_id": str(script.id),
            "project_id": str(project_id),
            "topic_ids": [str(topic.id) for topic in eligible],
        },
        idempotency_key=idempotency_key,
    )
    await audit_logs_service.record_audit_log(
        session,
        action=AUDIT_ACTION_ENQUEUED,
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=script.id,
        project_id=project_id,
        actor_id=owner_id,
        context={"job_id": str(job.id), "accepted_topic_count": len(eligible)},
    )
    return ScriptGenerationEnqueued(script=script, job=job, replayed=False)


def _insufficient_evidence_message(assessment: EvidenceAssessment, settings: Settings) -> str:
    """Why the evidence cannot carry the target, and what would fix it - written for the owner."""
    target = format_clock(assessment.target_duration_ms)
    defensible = format_clock(assessment.defensible_duration_ms)
    claims = (
        f"{assessment.supported_claims} supported claim"
        f"{'' if assessment.supported_claims == 1 else 's'}"
    )
    topics = f"{assessment.topic_count} accepted topic{'' if assessment.topic_count == 1 else 's'}"
    unused = assessment.supported_claims - assessment.usable_claims
    cap_note = (
        f" ({unused} more than the {settings.MAX_CLAIMS_PER_SECTION} a single topic can use "
        "were not counted)"
        if unused
        else ""
    )
    return (
        f"There is not enough verified evidence for a video of {target}. Your {claims} across "
        f"{topics}{cap_note} can carry about {defensible} of narration; stretching them to "
        f"{target} would mean repeating them or inventing facts, so nothing was generated. Add "
        "more source material and verify more claims, accept more topics, or choose a target "
        f"of {defensible} or shorter."
    )


async def _script_from_job(session: AsyncSession, job: JobRecord) -> ScriptRecord:
    script_id = UUID(job.payload["script_id"])
    script = await scripts_repo.get_by_id_for_project(session, script_id, job.project_id)
    if script is None:  # the job outlived its script - should never happen
        raise NotFoundError(f"Script {script_id} not found.")
    return script


async def _recover_stale_generations(session: AsyncSession, *, project_id: UUID) -> None:
    """Fail any ``generate_script`` job a crashed worker left ``running``, so it cannot
    block the project for ever. Mirrors
    ``topic_extraction._recover_stale_extractions`` /
    ``claim_extraction._recover_stale_extractions`` exactly: this handler also commits
    its "running" transition immediately and releases the job row's claim lock before
    generation begins (see the module docstring, "Progress"), so - like topic and claim
    extraction, unlike every other job kind - a crashed worker leaves nothing holding
    the row. Without this, that job sits ``running`` for ever and (pre-migration-0013)
    would also have permanently blocked a fresh generation via
    ``uq_scripts_project_id_active``.

    A job that is genuinely being worked on is never touched: recovery needs it to be
    older than ``SCRIPT_GENERATION_STALE_AFTER_SECONDS`` *and* not held by a live worker
    (see ``jobs.fail_stale_running``).

    Unlike the extraction services, ``enqueue_script_generation`` does not refuse a
    second concurrent generation (Phase 7 deliberately allows a fresh ``pending``
    generation to coexist with the still-``approved`` script while it runs) - this call
    only recovers abandoned jobs, it never blocks a new one.
    """
    stale_after = get_settings().SCRIPT_GENERATION_STALE_AFTER_SECONDS
    recovered = await jobs_repo.fail_stale_running(
        session,
        project_id=project_id,
        kind=GENERATE_SCRIPT_JOB_KIND,
        stale_after=timedelta(seconds=stale_after),
        error=STALE_GENERATION_ERROR,
    )
    for job in recovered:
        log.warning(
            "scripts.stale_generation_recovered",
            job_id=str(job.id),
            project_id=str(project_id),
            started_at=job.started_at.isoformat() if job.started_at else None,
            stale_after_seconds=stale_after,
        )
        script_id_raw = job.payload.get("script_id")
        if script_id_raw is not None:
            await scripts_repo.set_status(
                session,
                project_id=project_id,
                script_id=UUID(str(script_id_raw)),
                status="failed",
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


# --- run (background job) --------------------------------------------------------


async def run_generate_script_job(
    session: AsyncSession,
    llm: LLM,
    *,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> JobRecord:
    """Process one queued ``generate_script`` job end to end.

    Marks it running and commits that immediately - releasing the row's lock
    before generation begins, so interim progress writes (through
    ``sessionmaker``, see the module docstring) never block on it - then:
    plans an outline, generates every topic's section(s), applies at most one
    bounded duration correction, validates the assembled result, and attaches
    it to the pending script. On success the job is ``succeeded`` and the
    script is ``draft``; on *any* failure both are ``failed`` and the error is
    sanitised, and any previously ``approved`` script is untouched (it was
    never superseded - see ``repositories.scripts.attach_draft``). Never
    raises for a generation failure - only :class:`NotFoundError` for a
    ``job_id`` that doesn't exist at all (a caller bug).
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
    # Release this row's lock now, before the (potentially long) generation
    # work below - otherwise every progress checkpoint's independent session
    # would simply queue up behind this one until the whole job finished.
    await session.commit()

    project_id = UUID(running.payload["project_id"])
    script_id = UUID(running.payload["script_id"])

    report = new_usage_report()
    try:
        result = await _generate_and_attach(
            session,
            llm,
            project_id=project_id,
            script_id=script_id,
            job_id=running.id,
            payload=running.payload,
            sessionmaker=sessionmaker,
            report=report,
        )
    except Exception as exc:  # noqa: BLE001 - any failure fails job + script, never raises
        await scripts_repo.set_status(
            session,
            project_id=project_id,
            script_id=script_id,
            status="failed",
            llm_model=llm.model,
        )
        error = describe_job_failure(exc, job_id=running.id, kind=GENERATE_SCRIPT_JOB_KIND)
        # A failed generation still paid for the calls it made: keep their usage.
        return await _mark_job_failed(
            session, job_id=running.id, error=error, result={"usage": report.as_dict()}
        )

    return await _mark_job_succeeded(session, job_id=running.id, result=result)


# --- generation pipeline (Phase 7) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SectionUnit:
    """One provider call's worth of section generation: one topic, or - when
    a topic's budget exceeds ``MAX_SECTION_DURATION_MS`` - one ordered
    sub-section of it (``domain.script_duration.split_topic_budget``). Every
    sub-section of one topic shares the same, already-capped claim list.
    """

    topic: ProjectTopicRecord
    claims: tuple[Claim, ...]
    duration_ms: int
    unit_index: int
    unit_count: int


def _parse_topic_ids(payload: dict[str, Any]) -> list[UUID]:
    raw = payload.get("topic_ids")
    if not isinstance(raw, list) or not raw:
        raise JobError("The generation job has no valid topic_ids in its payload.")
    try:
        return [UUID(str(item)) for item in raw]
    except (ValueError, AttributeError) as exc:
        raise JobError("The generation job has no valid topic_ids in its payload.") from exc


async def _report_progress(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    *,
    job_id: UUID,
    base_payload: dict[str, Any],
    stage: str,
    **extra: Any,
) -> None:
    """Report interim progress through a short, independent session - never
    the main generation session, and never anything but the job's own
    ``payload``. A slow or contended progress write can therefore never block
    generation, and can never carry (or block behind) partial script data,
    since nothing is written to ``scripts`` / ``script_segments`` /
    ``script_segment_claims`` until the single final ``attach_draft`` call
    far below. A no-op when ``sessionmaker`` is ``None`` (a caller with only a
    bare session simply gets no interim progress, never an error).
    """
    if sessionmaker is None:
        return
    progress = {"stage": stage, "updated_at": datetime.now(UTC).isoformat(), **extra}
    async with sessionmaker() as progress_session:
        alive = await jobs_repo.record_progress(
            progress_session, job_id=job_id, payload={**base_payload, "progress": progress}
        )
        await progress_session.commit()
    if not alive:
        raise JobAbandonedError


def _build_units(
    topics: Sequence[ProjectTopicRecord],
    claims_by_topic: dict[UUID, list[ClaimVerificationRecord]],
    budget_by_topic: dict[UUID, int],
    settings: Settings,
) -> list[_SectionUnit]:
    units: list[_SectionUnit] = []
    for topic in topics:
        capped = claims_by_topic[topic.id][: settings.MAX_CLAIMS_PER_SECTION]
        provider_claims = tuple(Claim(id=str(claim.id), text=claim.claim_text) for claim in capped)
        chunks = split_topic_budget(
            budget_by_topic[topic.id], max_section_duration_ms=settings.MAX_SECTION_DURATION_MS
        )
        for index, chunk_ms in enumerate(chunks):
            units.append(
                _SectionUnit(
                    topic=topic,
                    claims=provider_claims,
                    duration_ms=chunk_ms,
                    unit_index=index,
                    unit_count=len(chunks),
                )
            )
    return units


def _next_topic_title(units: Sequence[_SectionUnit], index: int) -> str | None:
    """The title of the next *different* topic after ``units[index]`` - never
    the next sub-section of the same one, which gets the "continue the same
    topic" framing instead (``providers.llm._format_section_request``).
    """
    current_topic_id = units[index].topic.id
    for later in units[index + 1 :]:
        if later.topic.id != current_topic_id:
            return later.topic.title
    return None


def _tail(text: str, max_chars: int) -> str:
    stripped = " ".join(text.split())
    return stripped if len(stripped) <= max_chars else stripped[-max_chars:].lstrip()


def _outside_tolerance(estimated_ms: int, target_ms: int, tolerance: float) -> bool:
    return abs(estimated_ms - target_ms) > target_ms * tolerance


async def _generate_units(
    llm: LLM,
    brief: ScriptBrief,
    lesson_title: str,
    units: Sequence[_SectionUnit],
    outline_by_topic: dict[UUID, Any],
    settings: Settings,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    job_id: UUID,
    payload: dict[str, Any],
    report: UsageReport,
    kind: str,
) -> list[ScriptSegmentDraft]:
    """Generate every unit's section in order, stamping each returned segment
    with its unit's ``topic_id`` (never taken from the model) and carrying
    bounded continuity forward from one unit to the next. Returns the
    segments in generation order (local per-call indices; the caller
    re-indexes across the whole assembled script). Every call's token usage is
    recorded in ``report`` under ``kind`` (``script_section`` for the first pass,
    ``script_correction`` for the bounded correction pass).
    """
    topic_order = list(dict.fromkeys(unit.topic.id for unit in units))
    topic_index_of = {topic_id: index for index, topic_id in enumerate(topic_order)}

    all_segments: list[ScriptSegmentDraft] = []
    previous_topic_title: str | None = None
    previous_tail: str | None = None

    for index, unit in enumerate(units):
        await _report_progress(
            sessionmaker,
            job_id=job_id,
            base_payload=payload,
            stage="generating_topic",
            topic_index=topic_index_of[unit.topic.id],
            topic_count=len(topic_order),
            topic_title=unit.topic.title,
            unit_index=unit.unit_index,
            unit_count=unit.unit_count,
        )

        outline_info = outline_by_topic.get(unit.topic.id)
        topic_brief = SectionTopicBrief(
            topic_id=unit.topic.id,
            title=unit.topic.title,
            summary=unit.topic.summary,
            keywords=tuple(unit.topic.keywords),
            purpose=outline_info.purpose if outline_info else None,
            key_concepts=outline_info.key_concepts if outline_info else (),
        )
        continuity = SectionContinuity(
            lesson_title=lesson_title,
            previous_topic_title=previous_topic_title,
            previous_section_tail=previous_tail,
            next_topic_title=_next_topic_title(units, index),
        )

        segments = await record_call(
            report,
            kind,
            llm.compose_script_section(
                brief,
                topic_brief,
                list(unit.claims),
                continuity,
                duration_ms=unit.duration_ms,
                unit_index=unit.unit_index,
                unit_count=unit.unit_count,
                max_segments=settings.MAX_SCRIPT_SEGMENTS,
                max_claims=settings.MAX_CLAIMS_PER_SECTION,
                words_per_minute=settings.SCRIPT_WORDS_PER_MINUTE,
            ),
        )

        stamped = [replace(segment, topic_id=unit.topic.id) for segment in segments]
        all_segments.extend(stamped)
        if unit.unit_index == unit.unit_count - 1:
            previous_topic_title = unit.topic.title
        previous_tail = _tail(stamped[-1].narration_text, settings.CONTINUITY_CONTEXT_CHARS)

    return all_segments


async def _generate_and_attach(
    session: AsyncSession,
    llm: LLM,
    *,
    project_id: UUID,
    script_id: UUID,
    job_id: UUID,
    payload: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    report: UsageReport,
) -> dict[str, object]:
    settings = get_settings()

    project = await projects_repo.get_by_id(session, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")

    topic_ids = _parse_topic_ids(payload)
    all_topics = await project_topics_repo.list_all_by_project(session, project_id)
    topics_by_id = {topic.id: topic for topic in all_topics}
    topics = [topics_by_id[topic_id] for topic_id in topic_ids if topic_id in topics_by_id]
    if not topics:
        raise JobError("None of this generation's accepted topics could be found any more.")

    await _report_progress(
        sessionmaker, job_id=job_id, base_payload=payload, stage="preparing_outline"
    )

    claims_by_topic: dict[UUID, list[ClaimVerificationRecord]] = {}
    for topic in topics:
        supported = await claim_verifications_repo.list_supported_by_topic(
            session, project_id=project_id, topic_id=topic.id
        )
        # Defence in depth: enqueue-time already required every one of these
        # topics to have supported evidence; claims are append-only, so this
        # should never actually be empty by the time the job runs.
        if not supported:
            raise ValidationError(
                f"Topic '{topic.title}' has no verified evidence; generate again once it does."
            )
        claims_by_topic[topic.id] = supported

    budgets = allocate_topic_budgets(
        topic_ids=[topic.id for topic in topics],
        claim_counts={topic_id: len(claims) for topic_id, claims in claims_by_topic.items()},
        target_duration_ms=project.target_duration_ms,
        min_section_duration_ms=settings.MIN_SECTION_DURATION_MS,
    )
    budget_by_topic = {budget.topic_id: budget.duration_ms for budget in budgets}

    units = _build_units(topics, claims_by_topic, budget_by_topic, settings)

    call_budget = max_provider_calls(
        max_target_duration_ms=MAX_TARGET_DURATION_MS,
        max_section_duration_ms=settings.MAX_SECTION_DURATION_MS,
        max_script_sections=settings.MAX_SCRIPT_SECTIONS,
    )
    if 1 + 2 * len(units) > call_budget:
        # Enforced, not merely documented (domain.script_duration.max_provider_calls):
        # this should never trip given enqueue's own MAX_SCRIPT_SECTIONS / duration
        # checks, but a generation is refused rather than left to run unbounded.
        raise JobError(
            "This generation would need more provider calls than are allowed; reduce "
            "the number of accepted topics or the target duration."
        )

    brief = ScriptBrief(topic=project.title, language=project.language)
    outline_topics = [
        OutlineTopicBrief(
            topic_id=topic.id,
            title=topic.title,
            summary=topic.summary,
            keywords=tuple(topic.keywords),
            duration_ms=budget_by_topic[topic.id],
        )
        for topic in topics
    ]
    outline_draft = await record_call(
        report, "script_outline", llm.compose_script_outline(brief, outline_topics)
    )
    outline_by_topic = {topic.topic_id: topic for topic in outline_draft.topics}

    segments = await _generate_units(
        llm,
        brief,
        outline_draft.title,
        units,
        outline_by_topic,
        settings,
        sessionmaker=sessionmaker,
        job_id=job_id,
        payload=payload,
        report=report,
        kind="script_section",
    )

    estimated_total = sum(segment.estimated_duration_ms for segment in segments)
    correction_applied = False
    if settings.MAX_SCRIPT_CORRECTION_ATTEMPTS >= 1 and _outside_tolerance(
        estimated_total, project.target_duration_ms, settings.SCRIPT_DURATION_TOLERANCE
    ):
        await _report_progress(
            sessionmaker, job_id=job_id, base_payload=payload, stage="correcting_duration"
        )
        factor = project.target_duration_ms / estimated_total
        rescaled_units = [
            replace(unit, duration_ms=max(1, round(unit.duration_ms * factor))) for unit in units
        ]
        segments = await _generate_units(
            llm,
            brief,
            outline_draft.title,
            rescaled_units,
            outline_by_topic,
            settings,
            sessionmaker=sessionmaker,
            job_id=job_id,
            payload=payload,
            report=report,
            kind="script_correction",
        )
        estimated_total = sum(segment.estimated_duration_ms for segment in segments)
        correction_applied = True
        # Duration is a target, not a hard gate (rule: TTS does not exist yet,
        # so this is a WPM estimate): the result is accepted from here on
        # regardless of whether it is still outside tolerance.

    await _report_progress(
        sessionmaker, job_id=job_id, base_payload=payload, stage="validating_provenance"
    )

    reindexed = [replace(segment, segment_index=index) for index, segment in enumerate(segments)]
    supported_claim_ids = {UUID(claim.id) for unit in units for claim in unit.claims}
    draft = ScriptDraft(
        title=outline_draft.title, segments=reindexed, estimated_duration_ms=estimated_total
    )
    # What the wording may draw on: for each topic, its title and every claim the writer was shown
    # for it; for the lesson as a whole, the project's and the topics' own titles.
    grounding = GroundingReference(
        topic_texts={
            unit.topic.id: " ".join([unit.topic.title, *(claim.text for claim in unit.claims)])
            for unit in units
        },
        frame_text=" ".join([project.title, *(topic.title for topic in topics)]),
    )
    validated = filter_and_validate(
        draft, supported_claim_ids, settings.SCRIPT_WORDS_PER_MINUTE, grounding=grounding
    )

    await _report_progress(sessionmaker, job_id=job_id, base_payload=payload, stage="finalising")

    attached = await scripts_repo.attach_draft(
        session,
        project_id=project_id,
        script_id=script_id,
        draft=validated,
        llm_model=llm.model,
    )
    if attached is None:
        raise ConflictError(f"Script {script_id} is no longer pending; cannot attach a draft.")

    target = project.target_duration_ms
    difference = validated.estimated_duration_ms - target
    return {
        "script_id": str(script_id),
        "segment_count": len(validated.segments),
        "topic_count": len(topics),
        "estimated_duration_ms": validated.estimated_duration_ms,
        "target_duration_ms": target,
        "duration_difference_ms": difference,
        "duration_difference_pct": round(difference / target * 100, 1) if target else 0.0,
        "correction_applied": correction_applied,
        "input_tokens": report.total.input_tokens or 0,
        "output_tokens": report.total.output_tokens or 0,
        "usage": report.as_dict(),
        "llm_model": llm.model,
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


# --- read model -----------------------------------------------------------------


async def list_scripts(
    session: AsyncSession,
    *,
    project_id: UUID,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> Page[ScriptRecord]:
    """A project's scripts, newest first (keyset-paginated).

    Not ownership-scoped itself - callers check the project belongs to the
    caller first (rule 9), exactly as the ``claim-verifications`` and
    ``sources`` list routes do.
    """
    return await scripts_repo.list_by_project(session, project_id, limit=limit, cursor=cursor)


async def assemble_script_detail(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID, script_id: UUID
) -> ScriptDetail:
    """A script with its segments and, per segment, the verified claims it
    cites. Ownership-scoped (rule 9). Citations are batch-loaded: one query
    for the segment->claim links, one for the claim records, whatever the
    segment count.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")

    segments = await scripts_repo.list_segments(session, project_id=project_id, script_id=script_id)
    links = await scripts_repo.list_segment_claims_for_script(
        session, project_id=project_id, script_id=script_id
    )

    claim_ids_by_segment: dict[UUID, list[UUID]] = {}
    for link in links:
        claim_ids_by_segment.setdefault(link.script_segment_id, []).append(
            link.claim_verification_id
        )

    claims_by_id = {
        verification.id: verification
        for verification in await claim_verifications_repo.list_by_ids(
            session,
            project_id=project_id,
            verification_ids=[link.claim_verification_id for link in links],
        )
    }

    return ScriptDetail(
        script=script,
        segments=tuple(
            ScriptSegmentDetail(
                segment=segment,
                claims=tuple(
                    claims_by_id[claim_id]
                    for claim_id in claim_ids_by_segment.get(segment.id, [])
                    if claim_id in claims_by_id
                ),
            )
            for segment in segments
        ),
    )


# --- approve -------------------------------------------------------------------


async def approve_script(
    session: AsyncSession, *, project_id: UUID, owner_id: UUID, script_id: UUID
) -> ScriptRecord:
    """Advance a script from ``draft`` to ``approved``.

    Ownership-scoped (rule 9). Raises
    :class:`~edutoon.core.errors.ConflictError` if the script is in any
    state other than ``draft``. Supersedes any other still-active script for
    the project first (there should be none - ``attach_draft`` already did -
    but this keeps the "one active script" invariant self-healing), then
    audit-logs the approval.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")
    if script.status != "draft":
        raise ConflictError(f"Only a draft script can be approved; this one is '{script.status}'.")

    await scripts_repo.supersede_active(session, project_id=project_id, except_id=script_id)
    approved = await scripts_repo.set_status(
        session, project_id=project_id, script_id=script_id, status="approved"
    )
    assert approved is not None  # just fetched and ownership-checked

    await audit_logs_service.record_audit_log(
        session,
        action=AUDIT_ACTION_APPROVED,
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=script_id,
        project_id=project_id,
        actor_id=owner_id,
    )
    return approved
