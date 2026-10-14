"""Training module access: loading the facts, applying the one rule, and the seam to Step 3.

``decide_module_access`` (``domain/training_access.py``) is the only place that decides who may open
a module. This service gathers the facts it needs for one employee and applies it:

* :func:`require_module_access` - for a request that names one module (its video). Returns the
  module, or raises the **one** 404 (``MODULE_NOT_FOUND``) for every reason the rule calls
  ``NOT_FOUND`` - no such module, not enrolled, enrolment withdrawn, course not published, module
  not published - or ``ModuleLockedError`` (403 ``module_locked``, naming the blocking module) when
  the employee is enrolled and the module is real but an earlier one has not been passed;
* :func:`list_my_courses` - the employee's own course listing, each module marked ``locked`` or
  not by that same function, so the listing and the stream can never disagree.

``passed_module_ids`` reads Step 3's assessment results: a module is passed when the employee has a
submitted attempt with ``passed`` - the only thing it looks for, so a later failed retake can never
take a pass away (passing is sticky). It fails closed: on any doubt it returns fewer ids, never
more.

A module that has **no assessment** blocks nothing (decision 6): ``assessed_module_ids`` - the
published modules of the course that have one - goes to the rule with the passed set, and module N
opens once every lower-positioned module that has an assessment is passed. The listing and the
stream both go through this one function, so they cannot disagree.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import ModuleLockedError, NotFoundError
from edutoon.domain.training import ENROLMENT_ACTIVE, MODULE_NOT_FOUND, MODULE_PUBLISHED
from edutoon.domain.training_access import (
    ACCESS_ALLOWED,
    ACCESS_LOCKED,
    AccessDecision,
    ModuleRef,
    decide_module_access,
)
from edutoon.repositories import assessments as assessments_repo
from edutoon.repositories import training as training_repo
from edutoon.repositories.training import CourseRecord, ModuleRecord
from edutoon.services.users import UserRecord

MODULE_LOCKED_MESSAGE = "This module is locked until the earlier module has been completed."


async def passed_module_ids(
    session: AsyncSession, *, user_id: UUID, module_ids: Collection[UUID]
) -> frozenset[UUID]:
    """The subset of ``module_ids`` that ``user_id`` has passed: those with a submitted, passing
    attempt. Sticky - only a pass is ever looked for. Fails closed: fewer ids, never more."""
    return await assessments_repo.passed_module_ids(session, user_id=user_id, module_ids=module_ids)


@dataclass(frozen=True, slots=True)
class BlockingModule:
    """The earlier module in the way. It is in the employee's own course, so naming it is safe."""

    id: UUID
    position: int
    title: str


@dataclass(frozen=True, slots=True)
class MyModule:
    module: ModuleRecord
    blocking: BlockingModule | None
    """``None`` when the module is open; otherwise the first earlier module not yet passed."""

    @property
    def locked(self) -> bool:
        return self.blocking is not None


@dataclass(frozen=True, slots=True)
class MyCourse:
    course: CourseRecord
    modules: list[MyModule]


def _refs(modules: list[ModuleRecord]) -> list[ModuleRef]:
    return [ModuleRef(m.id, m.position, m.status) for m in modules]


def _blocking(modules: list[ModuleRecord], decision: AccessDecision) -> BlockingModule | None:
    blocker = next((m for m in modules if m.id == decision.blocking_module_id), None)
    if blocker is None:  # LOCKED always names a module of this course; refuse to guess otherwise
        return None
    return BlockingModule(blocker.id, blocker.position, blocker.title)


async def list_my_courses(session: AsyncSession, user: UserRecord) -> list[MyCourse]:
    """The courses the employee is actively enrolled in and that are published, each with its
    **published** modules in order and whether each is locked.

    A module that is not published, a course that is not published and a withdrawn enrolment are not
    listed at all - the same things ``require_module_access`` answers with a 404 for.
    """
    courses = await training_repo.list_open_courses_of_user(session, user.id)
    by_course = await training_repo.list_modules_of_courses(session, [c.id for c in courses])
    result: list[MyCourse] = []
    for course in courses:
        modules = by_course[course.id]
        published_ids = [m.id for m in modules if m.status == MODULE_PUBLISHED]
        passed = await passed_module_ids(session, user_id=user.id, module_ids=published_ids)
        assessed = await assessments_repo.module_ids_with_assessment(session, published_ids)
        visible: list[MyModule] = []
        for module in modules:
            decision = decide_module_access(
                module_id=module.id,
                user_role=user.role,
                user_status=user.status,
                course_status=course.status,
                enrolment_status=ENROLMENT_ACTIVE,
                course_modules=_refs(modules),
                passed_module_ids=passed,
                assessed_module_ids=assessed,
            )
            if decision.access == ACCESS_ALLOWED:
                visible.append(MyModule(module, None))
            elif decision.access == ACCESS_LOCKED:
                blocking = _blocking(modules, decision)
                if blocking is not None:
                    visible.append(MyModule(module, blocking))
            # NOT_FOUND (an unpublished module): not listed.
        result.append(MyCourse(course, visible))
    return result


async def require_module_access(
    session: AsyncSession, user: UserRecord, module_id: UUID
) -> ModuleRecord:
    """Return the module if ``user`` may open it; otherwise raise (see the module docstring).

    Every refusal that is not "locked" is the same ``NotFoundError(MODULE_NOT_FOUND)``: the route
    cannot tell the caller which of the five reasons applied.
    """
    module = await training_repo.get_module(session, module_id)
    course = await training_repo.get_course(session, module.course_id) if module else None
    enrolment = (
        await training_repo.get_enrolment(session, course_id=course.id, user_id=user.id)
        if course
        else None
    )
    siblings = await training_repo.list_modules(session, course.id) if course else []
    published_ids = [m.id for m in siblings if m.status == MODULE_PUBLISHED]
    passed = (
        await passed_module_ids(session, user_id=user.id, module_ids=published_ids)
        if course
        else frozenset()
    )
    assessed = await assessments_repo.module_ids_with_assessment(session, published_ids)
    decision = decide_module_access(
        module_id=module_id,
        user_role=user.role,
        user_status=user.status,
        course_status=course.status if course else None,
        enrolment_status=enrolment.status if enrolment else None,
        course_modules=_refs(siblings),
        passed_module_ids=passed,
        assessed_module_ids=assessed,
    )
    if decision.access == ACCESS_ALLOWED and module is not None:
        return module
    if decision.access == ACCESS_LOCKED:
        blocking = _blocking(siblings, decision)
        if blocking is not None:
            raise ModuleLockedError(
                MODULE_LOCKED_MESSAGE,
                details={
                    "blocking_module_id": str(blocking.id),
                    "blocking_module_position": blocking.position,
                    "blocking_module_title": blocking.title,
                },
            )
    raise NotFoundError(MODULE_NOT_FOUND)
