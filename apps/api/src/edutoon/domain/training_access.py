"""Who may open a training module: the one rule, pure.

``decide_module_access`` is the only place this is decided. Every employee route that touches a
module (Step 2: its video; Step 3: its assessment) calls the service that loads the facts and
applies this function - nothing else may decide, and no route decides for itself.

**Three outcomes, on purpose.**

* ``ALLOWED``;
* ``LOCKED`` - the employee is enrolled and the module is real, but an earlier module in the course
  has not been passed. The caller may be told which one (it is *their own* progress);
* ``NOT_FOUND`` - every other reason. There is deliberately **one** outcome for "no such module",
  "not enrolled", "enrolment withdrawn", "course not published", "module not published" and "not an
  active employee", so the caller *cannot* answer those differently and a non-enrolled employee
  learns nothing about which courses and modules exist (the route answers them with one identical
  404, body included). Rule 9, enforced by the shape of the return value.

**Fails closed.** Anything not positively established is a refusal: no enrolment fact, an
unrecognised status, a module that is not in the course's list, an empty pass set with a predecessor
- all refuse. A module is ``ALLOWED`` only when every condition below holds:

1. the user is an active employee;
2. the course is ``published`` and the user's enrolment in it is ``active``;
3. the module belongs to that course and is ``published``;
4. every *published* module of the course with a lower position **that has an assessment** has been
   passed. A module with no assessment blocks nothing (Step 3, decision 6): module N is open once
   every lower-positioned module that *has* one is passed. ``assessed_module_ids`` names the modules
   that have one; ``None`` ("not told") is the fail-closed reading - every module requires a pass.

The first published module has no predecessor, so it is open to an enrolled employee. A pass is
sticky: it is read from the employee's submitted attempts (see
``services.training_access.passed_module_ids``), and a later failed retake never removes it.

Pure: no I/O, no repositories, no clock (rule 1).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from edutoon.domain.roles import ROLE_EMPLOYEE, USER_STATUS_ACTIVE
from edutoon.domain.training import (
    COURSE_PUBLISHED,
    ENROLMENT_ACTIVE,
    MODULE_PUBLISHED,
)
from edutoon.domain.training_assessment import requires_pass

Access = Literal["allowed", "locked", "not_found"]

ACCESS_ALLOWED: Final[Access] = "allowed"
ACCESS_LOCKED: Final[Access] = "locked"
ACCESS_NOT_FOUND: Final[Access] = "not_found"


@dataclass(frozen=True, slots=True)
class ModuleRef:
    """One module of the course, as the access rule needs to see it."""

    id: UUID
    position: int
    status: str


@dataclass(frozen=True, slots=True)
class AccessDecision:
    access: Access
    blocking_module_id: UUID | None = None
    """For ``LOCKED``: the first earlier published module that has not been passed."""


NOT_FOUND = AccessDecision(ACCESS_NOT_FOUND)


def decide_module_access(
    *,
    module_id: UUID,
    user_role: str,
    user_status: str,
    course_status: str | None,
    enrolment_status: str | None,
    course_modules: Sequence[ModuleRef],
    passed_module_ids: Collection[UUID],
    assessed_module_ids: Collection[UUID] | None = None,
) -> AccessDecision:
    """Decide whether the user may open ``module_id``. See the module docstring.

    ``course_status`` and ``enrolment_status`` are ``None`` when there is no such course or no
    enrolment. ``course_modules`` is every module of the module's course (published or not).
    """
    if user_role != ROLE_EMPLOYEE or user_status != USER_STATUS_ACTIVE:
        return NOT_FOUND
    if course_status != COURSE_PUBLISHED or enrolment_status != ENROLMENT_ACTIVE:
        return NOT_FOUND

    target = next((m for m in course_modules if m.id == module_id), None)
    if target is None or target.status != MODULE_PUBLISHED:
        return NOT_FOUND

    passed = frozenset(passed_module_ids)
    earlier = sorted(
        (
            m
            for m in course_modules
            if m.status == MODULE_PUBLISHED and m.position < target.position
        ),
        key=lambda m: m.position,
    )
    for predecessor in earlier:
        if requires_pass(predecessor.id, assessed_module_ids) and predecessor.id not in passed:
            return AccessDecision(ACCESS_LOCKED, blocking_module_id=predecessor.id)
    return AccessDecision(ACCESS_ALLOWED)
