"""Course enrolments - the administrator's side (Step 2, Piece 3).

An enrolment is one row per ``(course, user)`` (``uq_course_enrolments_course_id_user_id``). It
is created active, **withdrawn** in place (the row is the learner's history and is never deleted
by this module) and **reactivated** in place if the same user is enrolled again - there is never
a second row.

**Who can be enrolled.** Only an account whose role is ``employee``
(:func:`domain.training.can_enrol`). The database cannot check another table's column, so this is
checked here, before anything is written; an administrator is refused with a 422 and no row
exists afterwards.

**Races.** The row is created by ``INSERT ... ON CONFLICT DO NOTHING`` against the unique
constraint, so the database - not a prior look - decides who wins when two requests enrol the
same person at once. The loser waits for the winner's transaction, then finds the row and answers
as the second of two requests for an active enrolment does (409). Enrolling holds a *shared* lock
on the course row, which lets enrolments proceed together but makes them wait for (and holds off)
a concurrent archive or delete of the course; withdrawing locks the one enrolment row.

**Idempotency policy.** The ``Idempotency-Key`` replays one request's result. A *different* key
that asks for something already true is a 409 (``already enrolled`` / ``already withdrawn``), the
same choice the module routes make for publishing a render twice.

Every change writes an audit row (``training.enrolment.created`` / ``.reactivated`` /
``.withdrawn``). The transaction is committed by the router's idempotency wrapper, after this
returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import ConflictError, NotFoundError, UnprocessableError
from edutoon.core.pagination import Page
from edutoon.domain.training import (
    COURSE_ARCHIVED,
    COURSE_NOT_FOUND,
    ENROLMENT_ACTIVE,
    ENROLMENT_NOT_FOUND,
    ENROLMENT_WITHDRAWN,
    USER_NOT_FOUND,
    EnrolAction,
    can_enrol,
    decide_enrolment,
    decide_withdrawal,
)
from edutoon.repositories import training as training_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.training import EnrolmentRecord
from edutoon.services import audit_logs as audit_logs_service

AUDIT_ENROLMENT = "course_enrolment"

NOT_AN_EMPLOYEE = "Only employee accounts can be enrolled in a course."
COURSE_ARCHIVED_FOR_ENROLMENT = "This course is archived, so nobody can be enrolled in it."
ALREADY_ENROLLED = "This employee is already enrolled in this course."
ALREADY_WITHDRAWN = "This employee's enrolment has already been withdrawn."


@dataclass(frozen=True, slots=True)
class EnrolmentOutcome:
    enrolment: EnrolmentRecord
    action: EnrolAction
    """``create`` - a new row; ``reactivate`` - the existing withdrawn row is active again."""


async def _audit(
    session: AsyncSession,
    *,
    actor_id: UUID,
    action: str,
    enrolment: EnrolmentRecord,
    **context: str,
) -> None:
    await audit_logs_service.record_audit_log(
        session,
        action=action,
        entity_type=AUDIT_ENROLMENT,
        actor_id=actor_id,
        entity_id=enrolment.id,
        context={
            "course_id": str(enrolment.course_id),
            "user_id": str(enrolment.user_id),
            **context,
        },
    )


async def _existing(session: AsyncSession, course_id: UUID, user_id: UUID) -> EnrolmentRecord:
    enrolment = await training_repo.get_enrolment(
        session, course_id=course_id, user_id=user_id, for_update=True
    )
    assert enrolment is not None, "the enrolment row must exist after a conflicting insert"
    return enrolment


async def enrol_user(
    session: AsyncSession, *, actor_id: UUID, course_id: UUID, user_id: UUID
) -> EnrolmentOutcome:
    """Enrol an employee, or reactivate their withdrawn enrolment. See the module docstring."""
    course = await training_repo.get_course(session, course_id, for_share=True)
    if course is None:
        raise NotFoundError(COURSE_NOT_FOUND)
    if course.status == COURSE_ARCHIVED:
        raise ConflictError(COURSE_ARCHIVED_FOR_ENROLMENT)

    user = await users_repo.get_by_id(session, user_id)
    if user is None:
        raise NotFoundError(USER_NOT_FOUND)
    if not can_enrol(user.role):
        raise UnprocessableError(NOT_AN_EMPLOYEE)

    current = await training_repo.get_enrolment(
        session, course_id=course_id, user_id=user_id, for_update=True
    )
    action = decide_enrolment(current.status if current else None)
    if action == "create":
        created = await training_repo.insert_enrolment_if_absent(
            session, course_id=course_id, user_id=user_id, assigned_by=actor_id
        )
        if created is None:
            # Lost a race: another request enrolled this person between our look and our insert.
            current = await _existing(session, course_id, user_id)
            action = decide_enrolment(current.status)
    if action == "reactivate":
        assert current is not None
        await training_repo.set_enrolment_status(
            session, current.id, status=ENROLMENT_ACTIVE, assigned_by=actor_id
        )
    elif action == "already_active":
        raise ConflictError(ALREADY_ENROLLED)

    enrolment = await _existing(session, course_id, user_id)
    if action == "create":
        await _audit(
            session, actor_id=actor_id, action="training.enrolment.created", enrolment=enrolment
        )
    else:
        assert current is not None
        await _audit(
            session,
            actor_id=actor_id,
            action="training.enrolment.reactivated",
            enrolment=enrolment,
            previous_assigned_by=str(current.assigned_by),
        )
    return EnrolmentOutcome(enrolment, action)


async def withdraw_user(
    session: AsyncSession, *, actor_id: UUID, course_id: UUID, user_id: UUID
) -> EnrolmentRecord:
    """Withdraw an employee: the row stays (status ``withdrawn``), their access ends with the
    commit. Withdrawing an archived course's enrolment is allowed - it only removes access."""
    if await training_repo.get_course(session, course_id) is None:
        raise NotFoundError(COURSE_NOT_FOUND)
    current = await training_repo.get_enrolment(
        session, course_id=course_id, user_id=user_id, for_update=True
    )
    action = decide_withdrawal(current.status if current else None)
    if action == "not_found":
        raise NotFoundError(ENROLMENT_NOT_FOUND)
    if action == "already_withdrawn":
        raise ConflictError(ALREADY_WITHDRAWN)
    assert current is not None
    await training_repo.set_enrolment_status(session, current.id, status=ENROLMENT_WITHDRAWN)
    enrolment = await _existing(session, course_id, user_id)
    await _audit(
        session, actor_id=actor_id, action="training.enrolment.withdrawn", enrolment=enrolment
    )
    return enrolment


async def list_course_enrolments(
    session: AsyncSession, *, course_id: UUID, status: str | None, limit: int, cursor: str | None
) -> Page[EnrolmentRecord]:
    if await training_repo.get_course(session, course_id) is None:
        raise NotFoundError(COURSE_NOT_FOUND)
    return await training_repo.list_enrolments_for_course(
        session, course_id=course_id, status=status, limit=limit, cursor=cursor
    )


async def list_user_enrolments(
    session: AsyncSession, *, user_id: UUID, status: str | None, limit: int, cursor: str | None
) -> Page[EnrolmentRecord]:
    if await users_repo.get_by_id(session, user_id) is None:
        raise NotFoundError(USER_NOT_FOUND)
    return await training_repo.list_enrolments_for_user(
        session, user_id=user_id, status=status, limit=limit, cursor=cursor
    )
