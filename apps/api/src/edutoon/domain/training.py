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

TITLE_MAX_CHARS: Final = 200
DESCRIPTION_MAX_CHARS: Final = 4000


def reorder_mismatch(current_ids: Sequence[UUID], requested_ids: Sequence[UUID]) -> bool:
    """Whether ``requested_ids`` fails to be a permutation of ``current_ids``.

    A reorder must name every module of the course exactly once; anything else (a module left
    out, one that is not the course's, a repeat) would leave the positions with a gap or a
    stranger in them, so it is refused whole.
    """
    return len(requested_ids) != len(current_ids) or set(requested_ids) != set(current_ids)
