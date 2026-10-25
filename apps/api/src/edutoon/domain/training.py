"""Training courses, modules and enrolments: the vocabulary.

The values mirror the ``CHECK`` constraints of migration 0020 (``ck_training_courses_status``,
``ck_training_modules_status``, ``ck_course_enrolments_status``); the migration stays the source of
truth for what the database accepts and these constants are how the rest of the code names them.

A **course** is an ordered container of **modules**; a module is one published video, owned by
exactly one course. An employee reaches a module only through an active **enrolment** in its
course (course-level only - there is no module-level enrolment). Who may open a given module is
decided in one place, :mod:`edutoon.domain.training_access`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal, get_args
from uuid import UUID

from edutoon.domain.roles import ROLE_EMPLOYEE

CourseStatus = Literal["draft", "published", "archived"]
"""Only a ``published`` course is visible to employees. A course that has learner history is
*archived*, never deleted (Step 3's assessment tables reference its modules with RESTRICT)."""

ModuleStatus = Literal["published", "unpublished"]
"""An ``unpublished`` module has no video file any more and is unreachable by employees."""

EnrolmentStatus = Literal["active", "withdrawn"]
"""Withdrawing keeps the row (and, from Step 3, the learner's history)."""

COURSE_DRAFT: Final[CourseStatus] = "draft"
COURSE_PUBLISHED: Final[CourseStatus] = "published"
COURSE_ARCHIVED: Final[CourseStatus] = "archived"

MODULE_PUBLISHED: Final[ModuleStatus] = "published"
MODULE_UNPUBLISHED: Final[ModuleStatus] = "unpublished"

ENROLMENT_ACTIVE: Final[EnrolmentStatus] = "active"
ENROLMENT_WITHDRAWN: Final[EnrolmentStatus] = "withdrawn"

COURSE_STATUSES: Final[frozenset[str]] = frozenset(get_args(CourseStatus))
MODULE_STATUSES: Final[frozenset[str]] = frozenset(get_args(ModuleStatus))
ENROLMENT_STATUSES: Final[frozenset[str]] = frozenset(get_args(EnrolmentStatus))

FIRST_POSITION: Final = 1
"""Module positions start at 1 (``ck_training_modules_position_positive``)."""

COURSE_NOT_FOUND: Final = "Course not found."
MODULE_NOT_FOUND: Final = "Module not found."
"""The one fixed message for a missing (or, to a non-administrator, any) course or module.

Deliberately free of the requested id: an administrator asking for a course that does not exist and
a non-administrator asking for one that does get **byte-identical** 404 bodies, so nothing about
which courses and modules exist leaks through the wording."""

COURSE_DELETE_REFUSED: Final = (
    "This course cannot be deleted because it has enrolment history (withdrawn enrolments are "
    "kept as history too). Archive it instead: archiving hides the course from employees and "
    "keeps every module, enrolment and record."
)
"""The 409 for deleting a course that anyone has ever been enrolled in, or (from Step 3) that has
learner history. It says what to do instead, which is the point of the refusal."""

ENROLMENT_NOT_FOUND: Final = "Enrolment not found."
USER_NOT_FOUND: Final = "User not found."

ENROLLABLE_ROLE: Final = ROLE_EMPLOYEE
"""Only an employee takes a course. The database cannot check a column of another table, so the
service enforces this before writing anything (an administrator is refused with a 422)."""

TITLE_MAX_CHARS: Final = 200
DESCRIPTION_MAX_CHARS: Final = 4000


def reorder_mismatch(current_ids: Sequence[UUID], requested_ids: Sequence[UUID]) -> bool:
    """Whether ``requested_ids`` fails to be a permutation of ``current_ids``.

    A reorder must name every module of the course exactly once; anything else (a module left
    out, one that is not the course's, a repeat) would leave the positions with a gap or a
    stranger in them, so it is refused whole.
    """
    return len(requested_ids) != len(current_ids) or set(requested_ids) != set(current_ids)


EnrolAction = Literal["create", "reactivate", "already_active"]
WithdrawAction = Literal["withdraw", "already_withdrawn", "not_found"]


def can_enrol(role: str) -> bool:
    """Whether an account with this role may be enrolled in a course."""
    return role == ENROLLABLE_ROLE


def decide_enrolment(existing_status: str | None) -> EnrolAction:
    """What enrolling a user must do, given the status of the enrolment row they already have.

    * no row -> ``create`` one;
    * a ``withdrawn`` row -> ``reactivate`` that same row (never a second row: the pair
      ``(course, user)`` is unique, and the row is the learner's history);
    * an ``active`` row -> ``already_active`` (the service answers 409);
    * any status this code does not know -> ``already_active``, i.e. refuse and change nothing.
    """
    if existing_status is None:
        return "create"
    if existing_status == ENROLMENT_WITHDRAWN:
        return "reactivate"
    return "already_active"


def decide_withdrawal(existing_status: str | None) -> WithdrawAction:
    """What withdrawing a user must do, given the status of the enrolment row they have."""
    if existing_status is None:
        return "not_found"
    if existing_status == ENROLMENT_ACTIVE:
        return "withdraw"
    return "already_withdrawn"
