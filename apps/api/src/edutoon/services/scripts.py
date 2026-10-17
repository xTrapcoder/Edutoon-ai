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
from edutoon.domain import general_knowledge as general_knowledge_domain
from edutoon.domain.evidence_sufficiency import (
    EvidenceAssessment,
    assess_evidence,
    format_clock,
)
from edutoon.domain.generation_settings import MAX_TARGET_DURATION_MS
from edutoon.domain.llm_usage import UsageReport
from edutoon.domain.narration_support import (
    UnsupportedSentence,
    split_sentences,
    without_sentences,
)
from edutoon.domain.script import ScriptDraft, ScriptSegmentDraft
from edutoon.domain.script_duration import (
    allocate_topic_budgets,
    max_provider_calls,
    split_topic_budget,
)
from edutoon.domain.script_provenance import (
    WEAK_CITATION_COVERAGE,
    GroundingReference,
    UngroundedSegmentError,
    UnsourcedSegmentError,
    filter_and_validate,
    relevant_claim_ids,
    weak_citation_coverage,
)
from edutoon.domain.storyboard_provenance import content_words
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
from edutoon.services import general_knowledge as general_knowledge_service
from edutoon.services import instructions as instructions_service
from edutoon.services import projects as projects_service
from edutoon.services import spend_guard
from edutoon.services.job_errors import describe_job_failure
from edutoon.services.llm_usage import new_usage_report, record_call
from edutoon.services.narration_support import SupportLog, unsupported_in

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
            f"{len(eligible)} accepted topics cannot each receive a section of at least "
            f"{settings.MIN_SECTION_DURATION_MS // 1000} seconds within a "
            f"{format_clock(project.target_duration_ms)} target. Accept fewer topics, or "
            "increase the target duration."
        )

    missing_evidence: list[str] = []
    supported_counts: list[int] = []
    general_knowledge = await general_knowledge_service.applies_to(session, project)
    for topic in eligible:
        supported = await claim_verifications_repo.list_supported_by_topic(
            session,
            project_id=project_id,
            topic_id=topic.id,
            include_general_knowledge=general_knowledge,
        )
        supported_counts.append(len(supported))
        if not supported:
            missing_evidence.append(topic.title)
    if missing_evidence:
        names = ", ".join(f"'{title}'" for title in missing_evidence)
        raise ValidationError(
            f"These accepted topics have no verified evidence yet: {names}. Extract "
            "and verify claims for them, or reject them, before generating a script. A keyword "
            "you typed that your document does not cover shows as 'not in your document' under "
            "the keyword coverage: reject it to leave it out of the lesson."
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


class HollowSectionError(JobError):
    """A section whose passages cite none of the topic's verified claims, even after rewrites."""


MAX_SECTION_REWRITES = 2
"""How many times one section may be rewritten after the script's checks reject it. Bounded: each
rewrite is one more provider call, and a writer that cannot satisfy the checks fails the job."""


def _invalid_segments(
    segments: list[ScriptSegmentDraft],
    unit: _SectionUnit,
    *,
    settings: Settings,
    grounding: GroundingReference | None,
) -> list[tuple[int, Exception]]:
    """Every segment of one section that the script's own checks would reject, with why: the same
    :func:`~edutoon.domain.script_provenance.filter_and_validate` the whole script must pass,
    applied to each segment on its own so that *all* the problems are found, not just the first."""
    supported = {UUID(claim.id) for claim in unit.claims}
    texts = _claim_texts(unit.claims)
    problems: list[tuple[int, Exception]] = []
    for position, segment in enumerate(segments):
        try:
            filter_and_validate(
                ScriptDraft(title="", segments=[segment]),
                supported,
                settings.SCRIPT_WORDS_PER_MINUTE,
                grounding=grounding,
                claim_texts=texts,
            )
        except (UnsourcedSegmentError, UngroundedSegmentError) as exc:
            problems.append((position, exc))
    return problems


HOLLOW_SECTION_CORRECTION = (
    "No passage of this section cites a claim, so the topic is not explained from its verified "
    "claims at all. Explain the topic using its claims and list the claim ids each passage "
    "draws on - only claims that passage actually states."
)


def _claim_texts(claims: Sequence[Claim]) -> dict[UUID, str]:
    return {UUID(claim.id): claim.text for claim in claims}


def _is_hollow(segments: list[ScriptSegmentDraft], claims: Sequence[Claim] = ()) -> bool:
    """A section none of whose passages cites a claim it actually shares wording with. Every
    accepted topic has verified claims, so a section made only of hand-offs says nothing the source
    supports: the topic would be in the script by name and absent in substance. With ``claims``, a
    citation that shares no content word with its passage does not count (it is dropped from the
    script anyway)."""
    texts = _claim_texts(claims)
    for segment in segments:
        cited = [UUID(claim_id) for claim_id in segment.claim_ids]
        if texts:
            cited = list(relevant_claim_ids(segment.narration_text, cited, texts))
        if cited:
            return False
    return True


def _closest_claims(narration_text: str, claims: Sequence[Claim], limit: int = 2) -> list[Claim]:
    """The claims whose wording overlaps ``narration_text`` most, best first; a claim sharing
    nothing is never offered."""
    narration_words = content_words(narration_text)
    scored = [(len(narration_words & content_words(claim.text)), claim) for claim in claims]
    ranked = sorted((pair for pair in scored if pair[0] > 0), key=lambda pair: -pair[0])
    return [claim for _, claim in ranked[:limit]]


def _corrections_for(
    segments: list[ScriptSegmentDraft],
    problems: list[tuple[int, Exception]],
    claims: Sequence[Claim] = (),
) -> list[str]:
    """What to tell the writer was wrong, one line per rejected passage, in plain words: which
    passage (by position and opening words), which rule it broke and what would fix it. The fix is
    never "cite something else to make it pass" - only cite the claims it truly draws on, or take
    the wording out. For a passage without a usable citation the claims whose wording it most
    resembles are named (computed, not guessed), or it is said that none is close."""
    lines: list[str] = []
    for position, exc in problems:
        opening = " ".join(segments[position].narration_text.split()[:10])
        if isinstance(exc, UngroundedSegmentError):
            named = ", ".join(f"'{token}'" for _, token in exc.tokens[:5])
            lines.append(
                f'Passage {position + 1} ("{opening} ...") mentions {named}, which none of the '
                "claims contain. Use only wording the claims support; leave those out."
            )
        else:
            if segments[position].claim_ids:
                why = "cites claims that do not say what it says, and is not marked connective"
            else:
                why = "lists no claim ids and is not marked connective"
            closest = _closest_claims(segments[position].narration_text, claims)
            if closest:
                named = " | ".join(f'"{claim.text}"' for claim in closest)
                hint = (
                    f"The claims closest to it are: {named}. "
                    "Cite one only if the passage states it. "
                )
            else:
                hint = "None of the claims is close to it. "
            lines.append(
                f'Passage {position + 1} ("{opening} ...") {why}. {hint}'
                "If it is only an introduction, a transition or an analogy, keep it short, mark it "
                "connective and cite nothing - never cite a claim the passage does not state."
            )
    return lines


def _general_knowledge_notice(topic_id: UUID, settings: Settings) -> ScriptSegmentDraft:
    """The fixed opening segment of a lesson based on general knowledge."""
    words = len(general_knowledge_domain.LESSON_NOTICE.split())
    return ScriptSegmentDraft(
        segment_index=0,
        narration_text=general_knowledge_domain.LESSON_NOTICE,
        claim_ids=[],
        estimated_duration_ms=round(words / settings.SCRIPT_WORDS_PER_MINUTE * 60_000),
        connective=True,
        topic_id=topic_id,
    )


def _claims_as_section(unit: _SectionUnit, settings: Settings) -> list[ScriptSegmentDraft]:
    """A section made of the topic's verified claims read out as they stand: one passage per claim
    (at most :data:`FALLBACK_CLAIMS_PER_SECTION`), each citing exactly its own claim. Plain, short
    and fully grounded - the fallback when the writer's own wording cannot be supported."""
    segments = []
    for claim in unit.claims[:FALLBACK_CLAIMS_PER_SECTION]:
        words = len(claim.text.split())
        segments.append(
            ScriptSegmentDraft(
                segment_index=0,
                narration_text=claim.text,
                claim_ids=[claim.id],
                estimated_duration_ms=round(words / settings.SCRIPT_WORDS_PER_MINUTE * 60_000),
                connective=False,
                topic_id=unit.topic.id,
            )
        )
    return segments


FALLBACK_CLAIMS_PER_SECTION = 6
"""The most verified claims a fallback section reads out for one topic."""


def _sentence_violates(
    sentence: str,
    segment: ScriptSegmentDraft,
    unit: _SectionUnit,
    *,
    settings: Settings,
    grounding: GroundingReference | None,
) -> str | None:
    """Why ``sentence`` alone breaks the figures-and-names rule (the names it brings in), or
    ``None``. It is checked as a passage of its own, exempt from the citation rule, with the same
    reference every passage is held to."""
    try:
        filter_and_validate(
            ScriptDraft(
                title="", segments=[replace(segment, narration_text=sentence, connective=True)]
            ),
            {UUID(claim.id) for claim in unit.claims},
            settings.SCRIPT_WORDS_PER_MINUTE,
            grounding=grounding,
            claim_texts=_claim_texts(unit.claims),
        )
    except UngroundedSegmentError as exc:
        named = ", ".join(f"'{token}'" for _, token in exc.tokens[:3])
        return f"mentions {named}, which none of the verified claims contain"
    return None


async def _without_rule_violations(
    llm: LLM,
    report: UsageReport,
    segments: list[ScriptSegmentDraft],
    problems: list[tuple[int, Exception]],
    *,
    unit: _SectionUnit,
    settings: Settings,
    grounding: GroundingReference | None,
    support: SupportLog | None,
) -> list[ScriptSegmentDraft]:
    """``segments`` with every sentence that still breaks a script rule taken out, then the rest
    judged against the claims like any other text. A passage with no citation goes whole; a passage
    that adds a figure or a name loses only the sentences that add it. Raises the first problem's
    own error if nothing usable is left, so a section with nothing valid in it still fails, saying
    which rule it broke."""
    removals: dict[int, list[UnsupportedSentence]] = {}
    for position, exc in problems:
        segment = segments[position]
        sentences = split_sentences(segment.narration_text)
        found: list[UnsupportedSentence] = []
        if isinstance(exc, UngroundedSegmentError):
            for index, sentence in enumerate(sentences):
                why = _sentence_violates(
                    sentence, segment, unit, settings=settings, grounding=grounding
                )
                if why is not None:
                    found.append(UnsupportedSentence(position=index, text=sentence, reason=why))
        if not found:
            # An uncited passage, or a violation not traceable to one sentence: the passage goes.
            found = [
                UnsupportedSentence(
                    position=index,
                    text=sentence,
                    reason=(
                        "the passage cites no verified claim"
                        if isinstance(exc, UnsourcedSegmentError)
                        else "the passage adds something its claims do not contain"
                    ),
                )
                for index, sentence in enumerate(sentences)
            ]
        removals[position] = found

    cleaned = _without_unsupported(
        segments,
        removals,
        words_per_minute=settings.SCRIPT_WORDS_PER_MINUTE,
        topic=unit.topic.title,
        support=support,
    )
    if not cleaned or _is_hollow(cleaned, unit.claims):
        raise problems[0][1]
    remaining = _invalid_segments(cleaned, unit, settings=settings, grounding=grounding)
    if remaining:
        raise remaining[0][1]
    if support is not None and support.enabled:
        judged = await _unsupported_by_segment(llm, report, cleaned, unit.claims, support)
        if judged:
            cleaned = _without_unsupported(
                cleaned,
                judged,
                words_per_minute=settings.SCRIPT_WORDS_PER_MINUTE,
                topic=unit.topic.title,
                support=support,
            )
            if not cleaned or _is_hollow(cleaned, unit.claims):
                raise problems[0][1]
    return cleaned


def _support_corrections(
    segments: list[ScriptSegmentDraft], unsupported: dict[int, list[UnsupportedSentence]]
) -> list[str]:
    """What to tell the writer about sentences no claim supports, one line per sentence: which
    passage, the sentence, and why the checker found no claim for it. The fix is never to cite a
    claim the sentence does not state - only to say what a claim says, or to leave it out."""
    lines: list[str] = []
    for position in sorted(unsupported):
        opening = " ".join(segments[position].narration_text.split()[:8])
        for found in unsupported[position]:
            lines.append(
                f'Passage {position + 1} ("{opening} ...") says "{found.text}" - no verified claim '
                f"supports it ({found.reason.strip().rstrip('.')}). Say only what a claim says, "
                "in the direction it says it, or leave the sentence out."
            )
    return lines


async def _unsupported_by_segment(
    llm: LLM,
    report: UsageReport,
    segments: list[ScriptSegmentDraft],
    claims: Sequence[Claim],
    support: SupportLog,
) -> dict[int, list[UnsupportedSentence]]:
    """For each segment (by position), the sentences no claim of its section supports."""
    found: dict[int, list[UnsupportedSentence]] = {}
    for position, segment in enumerate(segments):
        flagged = await unsupported_in(llm, report, segment.narration_text, claims, support)
        if flagged:
            found[position] = flagged
    return found


def _without_unsupported(
    segments: list[ScriptSegmentDraft],
    unsupported: dict[int, list[UnsupportedSentence]],
    *,
    words_per_minute: int,
    topic: str,
    support: SupportLog | None,
) -> list[ScriptSegmentDraft]:
    """``segments`` with the unsupported sentences taken out. A segment left too short to read out
    is dropped. Each removal is recorded on the job's result with the judge's reason."""
    kept: list[ScriptSegmentDraft] = []
    for position, segment in enumerate(segments):
        flagged = unsupported.get(position)
        if not flagged:
            kept.append(segment)
            continue
        sentences = split_sentences(segment.narration_text)
        remaining = without_sentences(sentences, [found.position for found in flagged])
        if support is not None:
            for found in flagged:
                support.removed.append(
                    {
                        "topic": topic,
                        "sentence": found.text,
                        "reason": found.reason,
                        "segment_dropped": remaining is None,
                    }
                )
        if remaining is None:
            continue
        words = len(remaining.split())
        kept.append(
            replace(
                segment,
                narration_text=remaining,
                estimated_duration_ms=round(words / words_per_minute * 60_000),
            )
        )
    return kept


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
    grounding: GroundingReference | None = None,
    retries: list[dict[str, object]] | None = None,
    support: SupportLog | None = None,
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

        async def _write(
            corrections: Sequence[str] = (),
            *,
            unit: _SectionUnit = unit,
            topic_brief: SectionTopicBrief = topic_brief,
            continuity: SectionContinuity = continuity,
        ) -> list[ScriptSegmentDraft]:
            drafted = await record_call(
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
                    **({"corrections": corrections} if corrections else {}),
                ),
            )
            return [replace(segment, topic_id=unit.topic.id) for segment in drafted]

        async def _assess(
            drafted: list[ScriptSegmentDraft],
            *,
            unit: _SectionUnit = unit,
        ) -> tuple[list[tuple[int, Exception]], bool, dict[int, list[UnsupportedSentence]]]:
            """What is wrong with ``drafted``: the script's own rule violations, a hollow section,
            and - only once those are clean, so no model call is spent on text that will be
            rewritten anyway - the sentences no verified claim of this topic supports."""
            found = _invalid_segments(drafted, unit, settings=settings, grounding=grounding)
            empty = _is_hollow(drafted, unit.claims)
            unsupported: dict[int, list[UnsupportedSentence]] = {}
            if support is not None and support.enabled and not found and not empty:
                unsupported = await _unsupported_by_segment(
                    llm, report, drafted, unit.claims, support
                )
            return found, empty, unsupported

        stamped = await _write()
        problems, hollow, unsupported = await _assess(stamped)
        rewrites = 0
        while (problems or hollow or unsupported) and rewrites < MAX_SECTION_REWRITES:
            # A bounded rewrite, told exactly what was wrong. Nothing is left out and nothing is
            # accepted on the strength of a rewrite: it must pass the very same checks, and if it
            # still does not after the last one the generation fails exactly as it always did.
            rewrites += 1
            corrections = _corrections_for(stamped, problems, unit.claims)
            if hollow:
                corrections.append(HOLLOW_SECTION_CORRECTION)
            corrections.extend(_support_corrections(stamped, unsupported))
            log.warning(
                "scripts.section_rewritten",
                topic=unit.topic.title,
                rejected=len(problems),
                hollow=hollow,
                unsupported=sum(len(found) for found in unsupported.values()),
                rewrite=rewrites,
            )
            if retries is not None:
                retries.append(
                    {
                        "topic": unit.topic.title,
                        "rewrite": rewrites,
                        "rejected_passages": len(problems),
                        "hollow": hollow,
                        "unsupported_sentences": sum(len(found) for found in unsupported.values()),
                        "problems": corrections,
                    }
                )
            if unsupported and support is not None:
                support.rewrites_for_support += 1
            stamped = await _write(corrections)
            problems, hollow, unsupported = await _assess(stamped)
        try:
            if hollow:
                raise HollowSectionError(
                    f"The section written for the topic '{unit.topic.title}' cites none of its "
                    "verified claims, so the topic is not explained from the source. Generate the "
                    "script again."
                )
            if problems:
                # Still breaking a script rule after the bounded rewrites. Nothing that breaks a
                # rule may reach the script - but a section is not thrown away for one sentence:
                # the offending sentences (or an uncited passage) are taken out, what is left is
                # judged, and only if nothing usable remains is the section replaced by its claims
                # (or, with the fallback off, the generation fails with the rule it broke).
                stamped = await _without_rule_violations(
                    llm,
                    report,
                    stamped,
                    problems,
                    unit=unit,
                    settings=settings,
                    grounding=grounding,
                    support=support,
                )
                unsupported = {}
            if unsupported:
                # Still not supported after the rewrites: what no claim backs is taken out, never
                # kept. The rest of the section is untouched and has already passed every check.
                stamped = _without_unsupported(
                    stamped,
                    unsupported,
                    words_per_minute=settings.SCRIPT_WORDS_PER_MINUTE,
                    topic=unit.topic.title,
                    support=support,
                )
                if not stamped or _is_hollow(stamped, unit.claims):
                    raise HollowSectionError(
                        f"Nothing the writer produced for the topic '{unit.topic.title}' was "
                        "supported by its verified claims. Generate the script again."
                    )
                remaining = _invalid_segments(stamped, unit, settings=settings, grounding=grounding)
                if remaining:
                    raise remaining[0][1]
        except (HollowSectionError, UngroundedSegmentError, UnsourcedSegmentError) as exc:
            if not settings.SCRIPT_FALLBACK_TO_CLAIMS:
                raise
            # The writer could not produce wording these claims support, even after rewrites and
            # removals. The topic is not dropped and nothing unsupported is kept: its verified
            # claims are read out as they stand (each one is backed by a cited passage), and the
            # job's result says so.
            stamped = _claims_as_section(unit, settings)
            if support is not None:
                support.fallback_sections.append(
                    {"topic": unit.topic.title, "claims": len(stamped), "reason": str(exc)}
                )
            log.warning("scripts.section_fell_back_to_claims", topic=unit.topic.title)
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
    general_knowledge = await general_knowledge_service.applies_to(session, project)
    for topic in topics:
        supported = await claim_verifications_repo.list_supported_by_topic(
            session,
            project_id=project_id,
            topic_id=topic.id,
            include_general_knowledge=general_knowledge,
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

    audience, tone = instructions_service.audience_and_tone(project)
    brief = ScriptBrief(
        topic=project.title,
        language=project.language,
        audience=audience,
        tone=tone,
        instructions=instructions_service.prompt_block(project),
        general_knowledge=general_knowledge,
    )
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

    # What the wording may draw on: for each topic, its title and every claim the writer was shown
    # for it; for the lesson as a whole, the project's and the topics' own titles.
    grounding = GroundingReference(
        topic_texts={
            unit.topic.id: " ".join([unit.topic.title, *(claim.text for claim in unit.claims)])
            for unit in units
        },
        frame_text=" ".join([project.title, *(topic.title for topic in topics)]),
    )
    retried: list[dict[str, object]] = []
    support = SupportLog(enabled=settings.SCRIPT_SUPPORT_CHECK_ENABLED)

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
        grounding=grounding,
        retries=retried,
        support=support,
    )

    estimated_total = sum(segment.estimated_duration_ms for segment in segments)
    correction_applied = False
    correction_failed: str | None = None
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
        corrected_retries: list[dict[str, object]] = []
        try:
            corrected = await _generate_units(
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
                grounding=grounding,
                retries=corrected_retries,
                support=support,
            )
        except (UnsourcedSegmentError, UngroundedSegmentError, HollowSectionError) as exc:
            # The correction pass only tunes the length; the first draft already passed every
            # section check. A rewrite that cannot satisfy those same checks is not accepted - and
            # it must not cost the valid draft either: the first draft is kept, unchanged, and the
            # reason is recorded. Nothing is relaxed: only checked text is ever used.
            correction_failed = str(exc)
            log.warning("scripts.correction_pass_failed", reason=correction_failed)
        else:
            segments = corrected
            # Only the pass that is kept describes the script that is kept.
            retried = corrected_retries
            estimated_total = sum(segment.estimated_duration_ms for segment in segments)
            correction_applied = True
        # Duration is a target, not a hard gate (rule: TTS does not exist yet,
        # so this is a WPM estimate): the result is accepted from here on
        # regardless of whether it is still outside tolerance.

    await _report_progress(
        sessionmaker, job_id=job_id, base_payload=payload, stage="validating_provenance"
    )

    if general_knowledge:
        # A lesson that rests on general knowledge says so aloud before anything else. The
        # sentence is fixed text (``domain.general_knowledge``), never written by the model.
        segments = [_general_knowledge_notice(topics[0].id, settings), *segments]
    reindexed = [replace(segment, segment_index=index) for index, segment in enumerate(segments)]
    supported_claim_ids = {UUID(claim.id) for unit in units for claim in unit.claims}
    draft = ScriptDraft(
        title=outline_draft.title, segments=reindexed, estimated_duration_ms=estimated_total
    )
    all_claim_texts = _claim_texts([claim for unit in units for claim in unit.claims])
    validated = filter_and_validate(
        draft,
        supported_claim_ids,
        settings.SCRIPT_WORDS_PER_MINUTE,
        grounding=grounding,
        claim_texts=all_claim_texts,
    )
    weak_citations = []
    for segment in validated.segments:
        coverage = weak_citation_coverage(
            segment.narration_text, segment.claim_verification_ids, all_claim_texts
        )
        if coverage is not None and coverage < WEAK_CITATION_COVERAGE:
            weak_citations.append(
                {"segment_index": segment.segment_index, "coverage": round(coverage, 2)}
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
        # Why the optional length-correction pass was dropped and the first draft kept, if it was.
        "correction_failed": correction_failed,
        # Sections whose first draft broke a script rule and were rewritten once, with what was
        # wrong; a rewrite that still broke a rule would have failed the generation.
        "section_retries": retried,
        # Cited segments whose cited claims cover little of their wording: for the reviewer's
        # second look, never a rejection (an analogy legitimately adds words).
        "weak_citation_segments": weak_citations,
        # True when the lesson rests on the model's general knowledge (a keyword-only project): its
        # claims were never checked against a document, and the script opens by saying so.
        "general_knowledge": general_knowledge,
        # The sentence-level support check (``domain.narration_support``): how many sentences were
        # judged against the verified claims, which were taken out and why, and how many could not
        # be judged. ``None`` when the check is switched off.
        "support_check": support.as_dict(),
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
