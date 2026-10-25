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

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import IdempotencyKey, RedisDep, StorageDep
from edutoon.api.policies import (
    require_employee,
    require_employee_for_assessment,
    require_employee_for_module,
)
from edutoon.api.schemas.assessments import (
    AttemptListResponse,
    AttemptResponse,
    AttemptSubmitRequest,
    EmployeeAssessmentResponse,
)
from edutoon.api.schemas.employee_training import MyCourseListResponse, MyCourseResponse
from edutoon.api.video_response import serve_module_video
from edutoon.db.session import get_session
from edutoon.domain.training import MODULE_NOT_FOUND
from edutoon.domain.training_assessment import Answer
from edutoon.services import assessments as assessments_service
from edutoon.services import idempotency as idempotency_service
from edutoon.services import training_access as access_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/training", tags=["training"])

Employee = Annotated[UserRecord, Depends(require_employee)]
ModuleEmployee = Annotated[UserRecord, Depends(require_employee_for_module)]
AssessmentEmployee = Annotated[UserRecord, Depends(require_employee_for_assessment)]
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


# --- assessments (Step 3) ----------------------------------------------------------------------
#
# Every route below calls ``assessments_service``, whose first act is ``require_module_access`` -
# the one rule. A 404 from it is re-raised as the assessment's own fixed 404, byte-identical for:
# no such module, not enrolled, withdrawn, course not published, module not published, a caller who
# is not an employee, and a module with no assessment. A locked module is the 403 ``module_locked``.

ASSESSMENT_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "The module is locked: an earlier module has not been passed."},
    404: {"description": "No such assessment, or it is not available to this employee."},
}


@router.get("/modules/{module_id}/assessment", responses=ASSESSMENT_RESPONSES)
async def get_assessment(
    module_id: UUID, employee: AssessmentEmployee, session: Session
) -> EmployeeAssessmentResponse:
    """The questions - never the answers."""
    return EmployeeAssessmentResponse.from_view(
        await assessments_service.get_for_employee(session, employee, module_id=module_id)
    )


@router.get("/modules/{module_id}/assessment/attempts", responses=ASSESSMENT_RESPONSES)
async def list_attempts(
    module_id: UUID, employee: AssessmentEmployee, session: Session
) -> AttemptListResponse:
    """The caller's own attempts at this module, oldest first."""
    attempts = await assessments_service.list_my_attempts(session, employee, module_id=module_id)
    return AttemptListResponse(items=[AttemptResponse.from_record(a) for a in attempts])


@router.post(
    "/modules/{module_id}/assessment/attempts",
    status_code=status.HTTP_201_CREATED,
    responses={**ASSESSMENT_RESPONSES, 422: {"description": "The answers cannot be graded."}},
)
async def submit_attempt(
    request: Request,
    module_id: UUID,
    body: AttemptSubmitRequest,
    employee: AssessmentEmployee,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> AttemptResponse:
    """Submit every answer at once. Graded and recorded in this request; a retry with the same
    ``Idempotency-Key`` returns the same attempt and never creates another."""
    answers = [Answer(a.question_id, a.option_id) for a in body.answers]

    async def _execute() -> tuple[int, Any]:
        attempt = await assessments_service.submit_attempt(
            session,
            employee,
            module_id=module_id,
            answers=answers,
            idempotency_key=idempotency_key,
        )
        return status.HTTP_201_CREATED, AttemptResponse.from_record(attempt).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=employee.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return AttemptResponse.model_validate(response_body)
