"""Training assessments: authoring (administrators) and attempts (employees).

**Lifecycle.** An assessment is a ``draft`` or ``published``. Saving (``PUT``) always produces a
draft and is refused (409) while the assessment is published - even with no attempts - so a live
assessment is never edited under an employee's feet. :func:`publish_assessment` re-validates the
whole assessment, then makes it visible to employees and lets it gate progression; it reports how
many active enrolments of the course that affects. :func:`unpublish_assessment` returns it to a
draft and is refused (409) once anyone has attempted it - which also freezes an attempted
assessment for good: its results refer to it exactly as it stands. Deleting is allowed for a draft
or a published assessment with no attempts, and refused (409) once it has attempts.

**Authoring** replaces a module's whole question set at once and changes its threshold (drafts
only); every attempt carries its own copy of the questions, options, key and threshold it was
graded with (migration 0021), so nothing recorded can change either way.

**Attempts.** An employee reads the questions (:func:`get_for_employee`, which returns types with
no field for the answer) and submits all their answers in one request (:func:`submit_attempt`). The
attempt is graded inside that request and written once, already graded - there is no open attempt
to modify afterwards and no function in the code base that updates or deletes one. Passing is
sticky: ``services.training_access.passed_module_ids`` only looks for a passing attempt, so a
later failed retake changes nothing.

**Access.** Every employee operation first calls ``require_module_access`` - the one rule - and a
refusal that is a 404 is re-raised as the assessment's own fixed 404 (``ASSESSMENT_NOT_FOUND``), the
same bytes as "this module has no assessment". A *draft* is refused the same way (see
:func:`_published_assessment`). So an assessment behind a module the caller cannot reach, or one
still being written, cannot be told apart from one that does not exist. A *locked* module stays
the 403 ``module_locked`` of the module routes (it is in the caller's own course listing already).

**Idempotency.** A submission carries an ``Idempotency-Key``. The router deduplicates through Redis
as every mutating route does; this service also makes the key durable: ``(user_id,
idempotency_key)`` is unique in the database, so a retry that finds its attempt returns *that*
attempt even if the cache has expired, and never creates a second.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import ConflictError, NotFoundError, UnprocessableError
from edutoon.domain.training import MODULE_NOT_FOUND
from edutoon.domain.training_assessment import (
    ASSESSMENT_ALREADY_DRAFT,
    ASSESSMENT_ALREADY_PUBLISHED,
    ASSESSMENT_DELETE_REFUSED,
    ASSESSMENT_DRAFT,
    ASSESSMENT_NOT_FOUND,
    ASSESSMENT_PUBLISHED,
    ASSESSMENT_PUBLISHED_REFUSED,
    ASSESSMENT_UNPUBLISH_REFUSED,
    Answer,
    AuthoredOption,
    AuthoredQuestion,
    answers_problem,
    authoring_problem,
    grade,
    publish_problem,
)
from edutoon.repositories import assessments as assessments_repo
from edutoon.repositories import training as training_repo
from edutoon.repositories.assessments import (
    AdminQuestion,
    AssessmentRecord,
    AttemptRecord,
    EmployeeQuestion,
)
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import training_access
from edutoon.services.users import UserRecord

AUDIT_ASSESSMENT = "training_assessment"
AUDIT_ATTEMPT = "training_assessment_attempt"

STALE_HINT = " If the assessment was just edited, reload it and answer again."


@dataclass(frozen=True, slots=True)
class AdminAssessmentView:
    assessment: AssessmentRecord
    questions: list[AdminQuestion]


@dataclass(frozen=True, slots=True)
class EmployeeAssessmentView:
    assessment: AssessmentRecord
    questions: list[EmployeeQuestion]


@dataclass(frozen=True, slots=True)
class PublishResult:
    view: AdminAssessmentView
    active_enrolments_affected: int
    """Active enrolments (withdrawn ones excluded) in the module's course when it was published:
    the employees for whom the pass gate now applies."""


# --- administrators ----------------------------------------------------------------------------


async def _lock_module(session: AsyncSession, module_id: UUID) -> UUID:
    """Lock the module's course, then the module (the lock order of ``services.training``).
    Returns the course's id."""
    first = await training_repo.get_module(session, module_id)
    if first is None:
        raise NotFoundError(MODULE_NOT_FOUND)
    course = await training_repo.get_course(session, first.course_id, for_update=True)
    module = await training_repo.get_module_locked(session, module_id)
    if course is None or module is None:
        raise NotFoundError(MODULE_NOT_FOUND)
    return course.id


async def _admin_view(session: AsyncSession, assessment: AssessmentRecord) -> AdminAssessmentView:
    return AdminAssessmentView(
        assessment, await assessments_repo.list_admin_questions(session, assessment.id)
    )


async def put_assessment(
    session: AsyncSession,
    *,
    actor_id: UUID,
    module_id: UUID,
    threshold_percent: int,
    questions: Sequence[AuthoredQuestion],
) -> AdminAssessmentView:
    """Create the module's assessment, or replace a draft's questions and threshold, all at once.

    The result is always a draft. A published assessment is refused (409) whether or not it has
    attempts: unpublish it first."""
    problem = authoring_problem(questions, threshold_percent)
    if problem is not None:
        raise UnprocessableError(problem)
    await _lock_module(session, module_id)
    existing = await assessments_repo.get_by_module(session, module_id)
    if existing is not None and existing.is_published:
        raise ConflictError(ASSESSMENT_PUBLISHED_REFUSED)
    assessment = await assessments_repo.upsert(
        session, module_id=module_id, threshold_percent=threshold_percent, created_by=actor_id
    )
    await assessments_repo.replace_questions(
        session, assessment_id=assessment.id, questions=questions
    )
    await audit_logs_service.record_audit_log(
        session,
        action="training.assessment.saved",
        entity_type=AUDIT_ASSESSMENT,
        actor_id=actor_id,
        entity_id=assessment.id,
        context={
            "module_id": str(module_id),
            "threshold_percent": threshold_percent,
            "question_count": len(questions),
            "status": assessment.status,
        },
    )
    return await _admin_view(session, assessment)


async def publish_assessment(
    session: AsyncSession, *, actor_id: UUID, module_id: UUID
) -> PublishResult:
    """Make a draft assessment live. The *whole* stored assessment is validated first (422), then
    it is published and the number of active enrolments in the course is reported (withdrawn ones
    are not counted). Publishing a published assessment is a 409.

    Publishing deliberately changes progression: from now on a module with this assessment holds
    back the modules after it until it is passed."""
    course_id = await _lock_module(session, module_id)
    assessment = await assessments_repo.get_by_module(session, module_id)
    if assessment is None:
        raise NotFoundError(ASSESSMENT_NOT_FOUND)
    if assessment.is_published:
        raise ConflictError(ASSESSMENT_ALREADY_PUBLISHED)
    stored = await assessments_repo.list_admin_questions(session, assessment.id)
    authored = [
        AuthoredQuestion(q.prompt, tuple(AuthoredOption(o.label, o.is_correct) for o in q.options))
        for q in stored
    ]
    problem = publish_problem(authored, assessment.threshold_percent)
    if problem is not None:
        raise UnprocessableError(problem)
    published = await assessments_repo.set_status(
        session, assessment.id, status=ASSESSMENT_PUBLISHED
    )
    affected = await training_repo.count_active_enrolments(session, course_id)
    await audit_logs_service.record_audit_log(
        session,
        action="training.assessment.published",
        entity_type=AUDIT_ASSESSMENT,
        actor_id=actor_id,
        entity_id=assessment.id,
        context={
            "module_id": str(module_id),
            "question_count": len(stored),
            "active_enrolments_affected": affected,
        },
    )
    return PublishResult(AdminAssessmentView(published, stored), affected)


async def unpublish_assessment(
    session: AsyncSession, *, actor_id: UUID, module_id: UUID
) -> AdminAssessmentView:
    """Return a published assessment to a draft - refused (409) once anyone has attempted it.

    The attempts check is made while holding the module row ``FOR UPDATE`` (``_lock_module``), which
    waits for any submission in flight - a submission holds the module ``FOR KEY SHARE`` from before
    it reads the assessment until it commits (:func:`submit_attempt`) - so an attempt cannot slip in
    between the check and the change, and a submission that arrives later finds a draft."""
    await _lock_module(session, module_id)
    assessment = await assessments_repo.get_by_module(session, module_id)
    if assessment is None:
        raise NotFoundError(ASSESSMENT_NOT_FOUND)
    if not assessment.is_published:
        raise ConflictError(ASSESSMENT_ALREADY_DRAFT)
    if await assessments_repo.has_attempts_for_module(session, module_id):
        raise ConflictError(ASSESSMENT_UNPUBLISH_REFUSED)
    draft = await assessments_repo.set_status(session, assessment.id, status=ASSESSMENT_DRAFT)
    await audit_logs_service.record_audit_log(
        session,
        action="training.assessment.unpublished",
        entity_type=AUDIT_ASSESSMENT,
        actor_id=actor_id,
        entity_id=assessment.id,
        context={"module_id": str(module_id)},
    )
    return await _admin_view(session, draft)


async def get_admin_view(session: AsyncSession, *, module_id: UUID) -> AdminAssessmentView:
    if await training_repo.get_module(session, module_id) is None:
        raise NotFoundError(MODULE_NOT_FOUND)
    assessment = await assessments_repo.get_by_module(session, module_id)
    if assessment is None:
        raise NotFoundError(ASSESSMENT_NOT_FOUND)
    return await _admin_view(session, assessment)


async def delete_assessment(session: AsyncSession, *, actor_id: UUID, module_id: UUID) -> None:
    """Remove a module's assessment - refused (409) once anyone has attempted it."""
    await _lock_module(session, module_id)
    assessment = await assessments_repo.get_by_module(session, module_id)
    if assessment is None:
        raise NotFoundError(ASSESSMENT_NOT_FOUND)
    if await assessments_repo.has_attempts_for_module(session, module_id):
        raise ConflictError(ASSESSMENT_DELETE_REFUSED)
    await assessments_repo.delete_assessment(session, assessment.id)
    await audit_logs_service.record_audit_log(
        session,
        action="training.assessment.deleted",
        entity_type=AUDIT_ASSESSMENT,
        actor_id=actor_id,
        entity_id=assessment.id,
        context={"module_id": str(module_id)},
    )


# --- employees ---------------------------------------------------------------------------------


async def _require_access(session: AsyncSession, user: UserRecord, module_id: UUID) -> None:
    """The one access rule, with its 404 re-worded as the assessment's own fixed 404."""
    try:
        await training_access.require_module_access(session, user, module_id)
    except NotFoundError as exc:
        raise NotFoundError(ASSESSMENT_NOT_FOUND) from exc


async def _published_assessment(session: AsyncSession, module_id: UUID) -> AssessmentRecord:
    """The module's assessment if it exists **and is published**; otherwise the assessment's own
    fixed 404 - the very one for "no such assessment", so a draft cannot be told from nothing."""
    assessment = await assessments_repo.get_by_module(session, module_id)
    if assessment is None or not assessment.is_published:
        raise NotFoundError(ASSESSMENT_NOT_FOUND)
    return assessment


async def get_for_employee(
    session: AsyncSession, user: UserRecord, *, module_id: UUID
) -> EmployeeAssessmentView:
    """The questions of a module's published assessment, as an employee may see them: no key."""
    await _require_access(session, user, module_id)
    assessment = await _published_assessment(session, module_id)
    return EmployeeAssessmentView(
        assessment, await assessments_repo.list_employee_questions(session, assessment.id)
    )


async def list_my_attempts(
    session: AsyncSession, user: UserRecord, *, module_id: UUID
) -> list[AttemptRecord]:
    await _require_access(session, user, module_id)
    await _published_assessment(session, module_id)
    return await assessments_repo.list_attempts(session, user_id=user.id, module_id=module_id)


async def submit_attempt(
    session: AsyncSession,
    user: UserRecord,
    *,
    module_id: UUID,
    answers: Sequence[Answer],
    idempotency_key: str,
) -> AttemptRecord:
    """Grade a submission and record it, or return the attempt its ``idempotency_key`` made.

    One transaction: hold the module row ``FOR KEY SHARE`` (so an administrator's edit, publish or
    unpublish cannot land half-way), serialise this employee's submissions for the module, find an
    earlier attempt with the key (a retry), otherwise grade against the assessment as it is *now*,
    and insert the attempt together with the exact question set and threshold used. A submission
    that cannot be graded (missing, repeated, unknown or foreign answers) is a 422 and writes
    nothing.
    """
    await _require_access(session, user, module_id)
    # Lock the module first - the same order as every administrator operation - so that an
    # unpublish racing this submission is wholly before it (we then find a draft: 404) or wholly
    # after it (it then sees our attempt: 409), and the two can never deadlock.
    await assessments_repo.lock_module_for_attempt(session, module_id)
    assessment = await _published_assessment(session, module_id)
    await assessments_repo.lock_user_module(session, user_id=user.id, module_id=module_id)

    earlier = await assessments_repo.get_attempt_by_key(
        session, user_id=user.id, idempotency_key=idempotency_key
    )
    if earlier is not None:
        if earlier.module_id != module_id:
            raise ConflictError("This Idempotency-Key was already used for a different request.")
        return earlier

    questions = await assessments_repo.load_grading_set(session, assessment.id)
    problem = answers_problem(questions, answers)
    if problem is not None:
        raise UnprocessableError(problem + STALE_HINT)
    result = grade(questions, answers, threshold_percent=assessment.threshold_percent)
    attempt = await assessments_repo.insert_attempt(
        session,
        assessment_id=assessment.id,
        module_id=module_id,
        user_id=user.id,
        attempt_number=await assessments_repo.next_attempt_number(
            session, user_id=user.id, module_id=module_id
        ),
        grade=result,
        idempotency_key=idempotency_key,
    )
    await audit_logs_service.record_audit_log(
        session,
        action="training.assessment.attempt_submitted",
        entity_type=AUDIT_ATTEMPT,
        actor_id=user.id,
        entity_id=attempt.id,
        context={
            "module_id": str(module_id),
            "attempt_number": attempt.attempt_number,
            "passed": attempt.passed,
        },
    )
    return attempt
