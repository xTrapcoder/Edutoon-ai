"""``/v1/training`` - what an enrolled employee sees of their own courses (Step 2, Piece 3).

Two routes, both read-only, both for employees:

* ``GET /courses`` - the employee's own courses and, for each, its published modules with an
  explicit ``locked`` flag and the module that blocks a locked one. (There is no user id in the path
  or the query: the caller is always the authenticated employee, so nobody can ask for another
  person's courses.)
* ``GET /modules/{module_id}/video`` - stream a module. Access is decided by
  ``services.training_access.require_module_access`` (the domain rule ``decide_module_access``) and
  nothing here:

  - allowed: the video is served by ``serve_module_video`` - the very function the administrator's
    preview uses - from the module's own snapshot;
  - **404** (always the identical body) for: no such module, not enrolled, enrolment withdrawn,
    course not published, module not published (and for a caller who is not an employee);
  - **403 ``module_locked``**, with the blocking module named, for an enrolled employee whose
    earlier module is not yet passed. That is the only 403 about a resource: the module is already
    in their own listing, so saying it is locked reveals nothing.

Nothing here mutates anything, so no ``Idempotency-Key`` applies. Access is checked when each
request arrives: a withdrawal takes effect for the next request (a download already in flight
finishes).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import StorageDep
from edutoon.api.policies import require_employee, require_employee_for_module
from edutoon.api.schemas.employee_training import MyCourseListResponse, MyCourseResponse
from edutoon.api.video_response import serve_module_video
from edutoon.db.session import get_session
from edutoon.domain.training import MODULE_NOT_FOUND
from edutoon.services import training_access as access_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/training", tags=["training"])

Employee = Annotated[UserRecord, Depends(require_employee)]
ModuleEmployee = Annotated[UserRecord, Depends(require_employee_for_module)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/courses")
async def list_my_courses(employee: Employee, session: Session) -> MyCourseListResponse:
    views = await access_service.list_my_courses(session, employee)
    return MyCourseListResponse(items=[MyCourseResponse.from_view(v) for v in views])


@router.get(
    "/modules/{module_id}/video",
    responses={
        403: {"description": "The module is locked: an earlier module has not been completed."},
        404: {"description": "No such module, or it is not available to this employee."},
    },
)
async def stream_module_video(
    module_id: UUID,
    employee: ModuleEmployee,
    session: Session,
    storage: StorageDep,
    range: Annotated[str | None, Header()] = None,  # noqa: A002 - the HTTP header's real name
) -> Response:
    await access_service.require_module_access(session, employee, module_id)
    return await serve_module_video(
        session,
        storage,
        module_id=module_id,
        range_header=range,
        not_found_message=MODULE_NOT_FOUND,
    )
