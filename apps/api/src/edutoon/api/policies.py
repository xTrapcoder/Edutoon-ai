"""Role policies: the one place a route's allowed role is declared.

Every route states who may call it by carrying exactly one of these as a dependency, normally on
its router (``APIRouter(..., dependencies=[Depends(require_admin)])``) so a new route in that
router inherits it, or on the route itself:

* :func:`require_admin` - an active administrator (anyone else: 403);
* :func:`require_admin_for_project` - the same for a route that names a project (anyone else: 404,
  identical whether or not the project exists);
* :func:`require_admin_for_course` / :func:`require_admin_for_module` - the same for a route that
  names a training course or module (anyone else: the fixed 404 an administrator gets for one that
  does not exist);
* :func:`require_employee` - an active employee (anyone else: 403);
* :func:`require_employee_for_module` - the same for a route that names a training module (anyone
  else: the fixed 404 an employee gets for a module they may not open);
* :func:`require_employee_for_assessment` - the same for the employee routes that name a module's
  assessment (anyone else: the fixed assessment 404, the one every unreachable case gets);
* :func:`require_authenticated` - any active user, whatever the role;
* :func:`public_route` - nobody is authenticated (``/health`` only).

``tests/test_route_policies.py`` walks every route of the real application and fails if one
declares none or more than one, so a route added without a decision cannot ship. The role comes
from ``users.role`` (loaded by ``get_current_user``) - never from the token or the request.

A failed role check is **403** (a capability) - except on a route that names a project, where it is
the same **404** rule 9 gives for a project that is not yours, so nothing about existence leaks.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends

from edutoon.api.dependencies import get_current_user
from edutoon.core.errors import ForbiddenError, NotFoundError
from edutoon.domain.roles import ROLE_ADMIN, ROLE_EMPLOYEE
from edutoon.domain.training import COURSE_NOT_FOUND, MODULE_NOT_FOUND
from edutoon.domain.training_assessment import ASSESSMENT_NOT_FOUND
from edutoon.services.users import UserRecord


async def public_route() -> None:
    """Marks a route that is deliberately open to anyone. Does nothing at runtime."""


async def require_authenticated(
    user: Annotated[UserRecord, Depends(get_current_user)],
) -> UserRecord:
    return user


async def require_admin(user: Annotated[UserRecord, Depends(get_current_user)]) -> UserRecord:
    if user.role != ROLE_ADMIN:
        raise ForbiddenError("This action needs an administrator account.")
    return user


async def require_admin_for_project(
    project_id: UUID, user: Annotated[UserRecord, Depends(get_current_user)]
) -> UserRecord:
    """Administrators only - and to anyone else the project simply does not exist.

    For a route that names a project (``.../{project_id}/...``). A non-administrator gets the same
    404, with the same message, an administrator gets for a project that is missing or not theirs
    (rule 9), whether or not that project exists: an employee can learn nothing about which
    projects there are. Routes with no project id (``GET`` / ``POST /v1/projects``, ``/v1/admin``)
    use :func:`require_admin` and answer 403.
    """
    if user.role != ROLE_ADMIN:
        raise NotFoundError(f"Project {project_id} not found.")
    return user


async def require_admin_for_course(
    course_id: UUID, user: Annotated[UserRecord, Depends(get_current_user)]
) -> UserRecord:
    """Administrators only - and to anyone else the training course simply does not exist.

    The message is the fixed ``COURSE_NOT_FOUND`` (no id echoed), the one an administrator gets for
    a course that is missing, so a non-administrator's 404 is byte-identical to it.
    """
    if user.role != ROLE_ADMIN:
        raise NotFoundError(COURSE_NOT_FOUND)
    return user


async def require_admin_for_module(
    module_id: UUID, user: Annotated[UserRecord, Depends(get_current_user)]
) -> UserRecord:
    """As :func:`require_admin_for_course`, for a route naming a training module."""
    if user.role != ROLE_ADMIN:
        raise NotFoundError(MODULE_NOT_FOUND)
    return user


async def require_employee(user: Annotated[UserRecord, Depends(get_current_user)]) -> UserRecord:
    if user.role != ROLE_EMPLOYEE:
        raise ForbiddenError("This action is for employee accounts.")
    return user


async def require_employee_for_module(
    module_id: UUID, user: Annotated[UserRecord, Depends(get_current_user)]
) -> UserRecord:
    """Employees only - and to anyone else the training module simply does not exist.

    For the employee routes that name a module. A non-employee gets the very 404 an employee gets
    for a module they may not open (no such module, not enrolled, withdrawn, course or module not
    published): the fixed ``MODULE_NOT_FOUND``. Whether the caller *may* open this module is then
    decided by ``services.training_access.require_module_access`` - never here.
    """
    if user.role != ROLE_EMPLOYEE:
        raise NotFoundError(MODULE_NOT_FOUND)
    return user


async def require_employee_for_assessment(
    module_id: UUID, user: Annotated[UserRecord, Depends(get_current_user)]
) -> UserRecord:
    """Employees only - and to anyone else the assessment simply does not exist.

    For the employee routes under ``/training/modules/{module_id}/assessment``. A non-employee gets
    the fixed ``ASSESSMENT_NOT_FOUND`` - the very 404 an employee gets when the module is unknown,
    unreachable (not enrolled, withdrawn, course or module not published) or has no assessment.
    Whether the caller *may* reach the module is then decided by
    ``services.training_access.require_module_access`` - never here.
    """
    if user.role != ROLE_EMPLOYEE:
        raise NotFoundError(ASSESSMENT_NOT_FOUND)
    return user


POLICY_NAMES: Final = {
    public_route: "public",
    require_authenticated: "authenticated",
    require_admin: "admin",
    require_admin_for_project: "admin_project",
    require_admin_for_course: "admin_course",
    require_admin_for_module: "admin_module",
    require_employee: "employee",
    require_employee_for_module: "employee_module",
    require_employee_for_assessment: "employee_assessment",
}
"""Every policy dependency and its short name; the route-policy test reads this."""
