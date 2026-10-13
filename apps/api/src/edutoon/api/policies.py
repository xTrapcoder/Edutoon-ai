"""Role policies: the one place a route's allowed role is declared.

Every route states who may call it by carrying exactly one of these as a dependency, normally on
its router (``APIRouter(..., dependencies=[Depends(require_admin)])``) so a new route in that
router inherits it, or on the route itself:

* :func:`require_admin` - an active administrator (anyone else: 403);
* :func:`require_admin_for_project` - the same for a route that names a project (anyone else: 404,
  identical whether or not the project exists);
* :func:`require_employee` - an active employee;
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


async def require_employee(user: Annotated[UserRecord, Depends(get_current_user)]) -> UserRecord:
    if user.role != ROLE_EMPLOYEE:
        raise ForbiddenError("This action is for employee accounts.")
    return user


POLICY_NAMES: Final = {
    public_route: "public",
    require_authenticated: "authenticated",
    require_admin: "admin",
    require_admin_for_project: "admin_project",
    require_employee: "employee",
}
"""Every policy dependency and its short name; the route-policy test reads this."""
