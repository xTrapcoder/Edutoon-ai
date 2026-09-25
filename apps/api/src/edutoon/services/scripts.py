"""Script Generation Engine - service layer (SG-4).

Ties the pieces built in SG-1..SG-3 together:

- ``enqueue_script_generation`` - the caller-facing entry point. Verifies
  project ownership, insists the project actually has something to say
  (>= 1 *supported* claim), supersedes any current active script, creates a
  fresh ``pending`` script plus a ``generate_script`` job, and writes an
  audit-log row. Idempotent on the caller's ``Idempotency-Key``: a repeat
  replays the original (script, job) instead of queuing a second run.
- ``run_generate_script_job`` - what the ``worker`` process
  (``edutoon/worker/``) calls after claiming the job. Loads the project's
  supported claims and their citations,
  asks ``providers.llm.compose_script`` for a draft, runs it through
  ``domain.script_provenance.filter_and_validate``, and attaches the
  validated result to the pending script (segments + claim links, status
  -> ``draft``). Any failure marks *both* the job and the script
  ``failed``; it never raises.
- ``assemble_script_detail`` - the read model: a script with its segments
  and, per segment, the verified claims it cites. Citations are batch-loaded
  (two queries total, regardless of segment count).
- ``approve_script`` - the ``draft -> approved`` transition, ownership
  checked, audit-logged.

Architecture (CLAUDE.md rules 1, 2, 3): this module is the only script layer
allowed to touch ``providers.llm``; it never issues SQL itself, going
through ``repositories`` for every read and write; every path is project +
owner scoped, and a resource the caller doesn't own is a 404, never a 403.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, NotFoundError, ValidationError
from edutoon.core.pagination import DEFAULT_PAGE_SIZE, Page
from edutoon.domain.script_provenance import filter_and_validate
from edutoon.providers.llm import LLM, Claim, ScriptBrief
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories.claim_verifications import ClaimVerificationRecord
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.scripts import ScriptRecord, ScriptSegmentRecord
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import projects as projects_service
from edutoon.services.job_errors import describe_job_failure

GENERATE_SCRIPT_JOB_KIND = "generate_script"

AUDIT_ENTITY_TYPE = "script"
AUDIT_ACTION_ENQUEUED = "script.generation_enqueued"
AUDIT_ACTION_APPROVED = "script.approved"


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
    owned by ``owner_id`` (rule 9), and
    :class:`~edutoon.core.errors.ValidationError` if the project has no
    supported claim for a script to cite. A repeat call with the same
    ``idempotency_key`` returns the original result with ``replayed=True``.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    existing_job = await jobs_repo.get_by_idempotency_key(
        session,
        project_id=project_id,
        kind=GENERATE_SCRIPT_JOB_KIND,
        idempotency_key=idempotency_key,
    )
    if existing_job is not None:
        script = await _script_from_job(session, existing_job)
        return ScriptGenerationEnqueued(script=script, job=existing_job, replayed=True)

    supported = await claim_verifications_repo.list_supported_by_project(session, project_id)
    if not supported:
        raise ValidationError(
            "This project has no supported claims yet - verify some source "
            "material before generating a script."
        )

    model = llm_model or get_settings().SCRIPT_LLM_MODEL

    await scripts_repo.supersede_active(session, project_id=project_id)
    script = await scripts_repo.create(session, project_id=project_id, llm_model=model)
    job = await jobs_repo.create(
        session,
        project_id=project_id,
        kind=GENERATE_SCRIPT_JOB_KIND,
        payload={"script_id": str(script.id), "project_id": str(project_id)},
        idempotency_key=idempotency_key,
    )
    await audit_logs_service.record_audit_log(
        session,
        action=AUDIT_ACTION_ENQUEUED,
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=script.id,
        project_id=project_id,
        actor_id=owner_id,
        context={"job_id": str(job.id), "supported_claim_count": len(supported)},
    )
    return ScriptGenerationEnqueued(script=script, job=job, replayed=False)


async def _script_from_job(session: AsyncSession, job: JobRecord) -> ScriptRecord:
    script_id = UUID(job.payload["script_id"])
    script = await scripts_repo.get_by_id_for_project(session, script_id, job.project_id)
    if script is None:  # the job outlived its script - should never happen
        raise NotFoundError(f"Script {script_id} not found.")
    return script


# --- run (background job) --------------------------------------------------------


async def run_generate_script_job(
    session: AsyncSession, llm: LLM, *, job_id: UUID
) -> JobRecord:
    """Process one queued ``generate_script`` job end to end.

    Marks it running, then: composes a draft from the project's supported
    claims, validates provenance, and attaches the result to the pending
    script. On success the job is ``succeeded`` and the script is ``draft``;
    on *any* failure both are ``failed`` and the error is recorded. Never
    raises for a generation failure - only :class:`NotFoundError` for a
    ``job_id`` that doesn't exist at all (a caller bug), matching
    ``evidence.run_build_evidence_job``.
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

    project_id = UUID(running.payload["project_id"])
    script_id = UUID(running.payload["script_id"])

    try:
        result = await _generate_and_attach(
            session, llm, project_id=project_id, script_id=script_id
        )
    except Exception as exc:  # noqa: BLE001 - any failure fails job + script, never raises
        await scripts_repo.set_status(
            session, project_id=project_id, script_id=script_id, status="failed"
        )
        error = describe_job_failure(exc, job_id=running.id, kind=GENERATE_SCRIPT_JOB_KIND)
        return await _mark_job_failed(session, job_id=running.id, error=error)

    return await _mark_job_succeeded(session, job_id=running.id, result=result)


async def _generate_and_attach(
    session: AsyncSession,
    llm: LLM,
    *,
    project_id: UUID,
    script_id: UUID,
) -> dict[str, object]:
    settings = get_settings()

    project = await projects_repo.get_by_id(session, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found.")

    supported = await claim_verifications_repo.list_supported_by_project(session, project_id)
    citations_by_claim = await claim_verifications_repo.list_citations_for(
        session, [verification.id for verification in supported]
    )
    # Defence in depth: only offer the writer a claim that is genuinely
    # cited. The Evidence Engine already downgrades an uncited "supported"
    # verdict, so this should never actually drop anything.
    citeable = [v for v in supported if citations_by_claim.get(v.id)]
    if not citeable:
        raise ValidationError("No cited supported claim is available for this project.")

    claims = [Claim(id=str(v.id), text=v.claim_text) for v in citeable]
    supported_claim_ids = {v.id for v in citeable}

    brief = ScriptBrief(topic=project.title)
    draft, usage = await llm.compose_script(
        brief,
        claims,
        max_segments=settings.MAX_SCRIPT_SEGMENTS,
        max_claims=settings.MAX_CLAIMS_PER_GENERATION,
        words_per_minute=settings.SCRIPT_WORDS_PER_MINUTE,
    )
    validated = filter_and_validate(
        draft, supported_claim_ids, settings.SCRIPT_WORDS_PER_MINUTE
    )

    attached = await scripts_repo.attach_draft(
        session, project_id=project_id, script_id=script_id, draft=validated
    )
    if attached is None:
        raise ConflictError(
            f"Script {script_id} is no longer pending; cannot attach a draft."
        )

    return {
        "script_id": str(script_id),
        "segment_count": len(validated.segments),
        "estimated_duration_ms": validated.estimated_duration_ms,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
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


async def _mark_job_failed(session: AsyncSession, *, job_id: UUID, error: str) -> JobRecord:
    updated = await jobs_repo.update(
        session,
        job_id=job_id,
        status="failed",
        finished_at=datetime.now(UTC),
        error=error,
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
    return await scripts_repo.list_by_project(
        session, project_id, limit=limit, cursor=cursor
    )


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

    segments = await scripts_repo.list_segments(
        session, project_id=project_id, script_id=script_id
    )
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
    the project first (there should be none - enqueue already did - but this
    keeps the "one active script" invariant self-healing), then audit-logs
    the approval.
    """
    await projects_service.get_project(session, project_id=project_id, owner_id=owner_id)

    script = await scripts_repo.get_by_id_for_project(session, script_id, project_id)
    if script is None:
        raise NotFoundError(f"Script {script_id} not found.")
    if script.status != "draft":
        raise ConflictError(
            f"Only a draft script can be approved; this one is '{script.status}'."
        )

    await scripts_repo.supersede_active(
        session, project_id=project_id, except_id=script_id
    )
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
