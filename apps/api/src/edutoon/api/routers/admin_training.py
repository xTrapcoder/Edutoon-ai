"""``/v1/admin/training`` - courses and modules. Administrators only.

Every mutating route requires an ``Idempotency-Key`` (rule 8) and is deduplicated against it, with
the transaction committed before the result is cached, as in ``routers/projects.py``. A mutation
that touches video files runs inside ``services.training.storage_effects``: a new file is removed
if the request fails before its commit, and a file the change no longer needs is removed only after
the commit succeeded (see ``services/training.py``).

There is deliberately no role check inside a handler. Each route carries exactly one policy
(``api/policies.py``): the collection routes answer a non-administrator 403, a route that names a
course or a module answers the fixed 404 an administrator gets for one that does not exist.
Modules are addressed by their own id (``/modules/{module_id}``), so a route never names two
resources whose 404 messages could differ.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import IdempotencyKey, RedisDep, StorageDep
from edutoon.api.policies import (
    require_admin,
    require_admin_for_course,
    require_admin_for_module,
)
from edutoon.api.schemas.assessments import AdminAssessmentResponse, AssessmentPutRequest
from edutoon.api.schemas.training import (
    CourseCreateRequest,
    CourseListResponse,
    CourseResponse,
    CourseStatusFilter,
    CourseUpdateRequest,
    EnrolmentCreateRequest,
    EnrolmentListResponse,
    EnrolmentResponse,
    EnrolmentStatusFilter,
    ModuleListResponse,
    ModuleOrderRequest,
    ModulePublishRequest,
    ModuleRepublishRequest,
    ModuleResponse,
)
from edutoon.api.video_response import serve_module_video
from edutoon.core.pagination import DEFAULT_PAGE_SIZE
from edutoon.db.session import get_session
from edutoon.domain.training_assessment import AuthoredOption, AuthoredQuestion
from edutoon.services import assessments as assessments_service
from edutoon.services import idempotency as idempotency_service
from edutoon.services import training as training_service
from edutoon.services import training_enrolments as enrolment_service
from edutoon.services.training import RenderSource
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/admin/training", tags=["admin-training"])

Admin = Annotated[UserRecord, Depends(require_admin)]
CourseAdmin = Annotated[UserRecord, Depends(require_admin_for_course)]
ModuleAdmin = Annotated[UserRecord, Depends(require_admin_for_module)]
Session = Annotated[AsyncSession, Depends(get_session)]


# --- courses -----------------------------------------------------------------------------


@router.post("/courses", status_code=status.HTTP_201_CREATED)
async def create_course(
    request: Request,
    body: CourseCreateRequest,
    admin: Admin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> CourseResponse:
    async def _execute() -> tuple[int, Any]:
        course = await training_service.create_course(
            session, actor_id=admin.id, title=body.title, description=body.description
        )
        return status.HTTP_201_CREATED, CourseResponse.from_record(course).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return CourseResponse.model_validate(response_body)


@router.get("/courses")
async def list_courses(
    admin: Admin,
    session: Session,
    course_status: Annotated[CourseStatusFilter | None, Query(alias="status")] = None,
    limit: Annotated[int, Query()] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> CourseListResponse:
    page = await training_service.list_courses(
        session, status=course_status, limit=limit, cursor=cursor
    )
    return CourseListResponse(
        items=[CourseResponse.from_record(course) for course in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/courses/{course_id}")
async def get_course(course_id: UUID, admin: CourseAdmin, session: Session) -> CourseResponse:
    return CourseResponse.from_record(
        await training_service.get_course(session, course_id=course_id)
    )


@router.patch("/courses/{course_id}")
async def update_course(
    request: Request,
    course_id: UUID,
    body: CourseUpdateRequest,
    admin: CourseAdmin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> CourseResponse:
    fields = body.model_dump(exclude_unset=True)
    if fields.get("title") is None:
        fields.pop("title", None)  # title is NOT NULL: an explicit null means "leave it"

    async def _execute() -> tuple[int, Any]:
        course = await training_service.update_course(
            session, actor_id=admin.id, course_id=course_id, fields=fields
        )
        return status.HTTP_200_OK, CourseResponse.from_record(course).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="PATCH",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return CourseResponse.model_validate(response_body)


@router.post("/courses/{course_id}/publish")
async def publish_course(
    request: Request,
    course_id: UUID,
    admin: CourseAdmin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> CourseResponse:
    async def _execute() -> tuple[int, Any]:
        course = await training_service.publish_course(
            session, actor_id=admin.id, course_id=course_id
        )
        return status.HTTP_200_OK, CourseResponse.from_record(course).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return CourseResponse.model_validate(response_body)


@router.post("/courses/{course_id}/archive")
async def archive_course(
    request: Request,
    course_id: UUID,
    admin: CourseAdmin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> CourseResponse:
    async def _execute() -> tuple[int, Any]:
        course = await training_service.archive_course(
            session, actor_id=admin.id, course_id=course_id
        )
        return status.HTTP_200_OK, CourseResponse.from_record(course).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return CourseResponse.model_validate(response_body)


@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_course(
    request: Request,
    course_id: UUID,
    admin: CourseAdmin,
    session: Session,
    redis: RedisDep,
    storage: StorageDep,
    idempotency_key: IdempotencyKey,
) -> None:
    async with training_service.storage_effects(storage) as effects:

        async def _execute() -> tuple[int, Any]:
            await training_service.delete_course(
                session, effects, actor_id=admin.id, course_id=course_id
            )
            return status.HTTP_204_NO_CONTENT, None

        await idempotency_service.run_with_idempotency(
            redis,
            owner_id=admin.id,
            method="DELETE",
            path=request.url.path,
            idempotency_key=idempotency_key,
            handler=_execute,
            commit=effects.wrap_commit(session.commit),
        )


# --- modules of a course ---------------------------------------------------------------------


@router.get("/courses/{course_id}/modules")
async def list_modules(course_id: UUID, admin: CourseAdmin, session: Session) -> ModuleListResponse:
    modules = await training_service.list_modules(session, course_id=course_id)
    return ModuleListResponse(items=[ModuleResponse.from_record(m) for m in modules])


@router.post("/courses/{course_id}/modules", status_code=status.HTTP_201_CREATED)
async def publish_module(
    request: Request,
    course_id: UUID,
    body: ModulePublishRequest,
    admin: CourseAdmin,
    session: Session,
    redis: RedisDep,
    storage: StorageDep,
    idempotency_key: IdempotencyKey,
) -> ModuleResponse:
    async with training_service.storage_effects(storage) as effects:

        async def _execute() -> tuple[int, Any]:
            module = await training_service.publish_module(
                session,
                effects,
                actor_id=admin.id,
                course_id=course_id,
                source=RenderSource(
                    project_id=body.project_id,
                    script_id=body.script_id,
                    orientation=body.orientation,
                    quality=body.quality,
                ),
                title=body.title,
                description=body.description,
            )
            return status.HTTP_201_CREATED, ModuleResponse.from_record(module).model_dump(
                mode="json"
            )

        _, response_body = await idempotency_service.run_with_idempotency(
            redis,
            owner_id=admin.id,
            method="POST",
            path=request.url.path,
            idempotency_key=idempotency_key,
            handler=_execute,
            commit=effects.wrap_commit(session.commit),
        )
    return ModuleResponse.model_validate(response_body)


@router.put("/courses/{course_id}/modules/order")
async def reorder_modules(
    request: Request,
    course_id: UUID,
    body: ModuleOrderRequest,
    admin: CourseAdmin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> ModuleListResponse:
    async def _execute() -> tuple[int, Any]:
        modules = await training_service.reorder_modules(
            session, actor_id=admin.id, course_id=course_id, module_ids=body.module_ids
        )
        response = ModuleListResponse(items=[ModuleResponse.from_record(m) for m in modules])
        return status.HTTP_200_OK, response.model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="PUT",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return ModuleListResponse.model_validate(response_body)


# --- enrolments --------------------------------------------------------------------------------


@router.post("/courses/{course_id}/enrolments", status_code=status.HTTP_201_CREATED)
async def enrol_user(
    request: Request,
    response: Response,
    course_id: UUID,
    body: EnrolmentCreateRequest,
    admin: CourseAdmin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> EnrolmentResponse:
    """Enrol an employee: 201 for a new enrolment, 200 when their withdrawn enrolment is made
    active again (the same row), 409 if they are already enrolled, 422 if the account is not an
    employee's."""

    async def _execute() -> tuple[int, Any]:
        outcome = await enrolment_service.enrol_user(
            session, actor_id=admin.id, course_id=course_id, user_id=body.user_id
        )
        code = status.HTTP_201_CREATED if outcome.action == "create" else status.HTTP_200_OK
        return code, EnrolmentResponse.from_record(outcome.enrolment).model_dump(mode="json")

    code, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    response.status_code = code
    return EnrolmentResponse.model_validate(response_body)


@router.get("/courses/{course_id}/enrolments")
async def list_course_enrolments(
    course_id: UUID,
    admin: CourseAdmin,
    session: Session,
    enrolment_status: Annotated[EnrolmentStatusFilter | None, Query(alias="status")] = None,
    limit: Annotated[int, Query()] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> EnrolmentListResponse:
    page = await enrolment_service.list_course_enrolments(
        session, course_id=course_id, status=enrolment_status, limit=limit, cursor=cursor
    )
    return EnrolmentListResponse(
        items=[EnrolmentResponse.from_record(e) for e in page.items], next_cursor=page.next_cursor
    )


@router.post("/courses/{course_id}/enrolments/{user_id}/withdraw")
async def withdraw_enrolment(
    request: Request,
    course_id: UUID,
    user_id: UUID,
    admin: CourseAdmin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> EnrolmentResponse:
    """End an employee's access to the course. The enrolment row is kept (status ``withdrawn``)."""

    async def _execute() -> tuple[int, Any]:
        enrolment = await enrolment_service.withdraw_user(
            session, actor_id=admin.id, course_id=course_id, user_id=user_id
        )
        return status.HTTP_200_OK, EnrolmentResponse.from_record(enrolment).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return EnrolmentResponse.model_validate(response_body)


@router.get("/enrolments")
async def list_user_enrolments(
    admin: Admin,
    session: Session,
    user_id: Annotated[UUID, Query()],
    enrolment_status: Annotated[EnrolmentStatusFilter | None, Query(alias="status")] = None,
    limit: Annotated[int, Query()] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> EnrolmentListResponse:
    """One user's enrolments across every course. ``user_id`` is a query parameter, so this is a
    collection route: an employee's 403 cannot depend on whether that user exists."""
    page = await enrolment_service.list_user_enrolments(
        session, user_id=user_id, status=enrolment_status, limit=limit, cursor=cursor
    )
    return EnrolmentListResponse(
        items=[EnrolmentResponse.from_record(e) for e in page.items], next_cursor=page.next_cursor
    )


# --- one module ----------------------------------------------------------------------------


@router.get("/modules/{module_id}")
async def get_module(module_id: UUID, admin: ModuleAdmin, session: Session) -> ModuleResponse:
    return ModuleResponse.from_record(
        await training_service.get_module(session, module_id=module_id)
    )


@router.post("/modules/{module_id}/republish")
async def republish_module(
    request: Request,
    module_id: UUID,
    body: ModuleRepublishRequest,
    admin: ModuleAdmin,
    session: Session,
    redis: RedisDep,
    storage: StorageDep,
    idempotency_key: IdempotencyKey,
) -> ModuleResponse:
    async with training_service.storage_effects(storage) as effects:

        async def _execute() -> tuple[int, Any]:
            module = await training_service.republish_module(
                session,
                effects,
                actor_id=admin.id,
                module_id=module_id,
                source=RenderSource(
                    project_id=body.project_id,
                    script_id=body.script_id,
                    orientation=body.orientation,
                    quality=body.quality,
                ),
            )
            return status.HTTP_200_OK, ModuleResponse.from_record(module).model_dump(mode="json")

        _, response_body = await idempotency_service.run_with_idempotency(
            redis,
            owner_id=admin.id,
            method="POST",
            path=request.url.path,
            idempotency_key=idempotency_key,
            handler=_execute,
            commit=effects.wrap_commit(session.commit),
        )
    return ModuleResponse.model_validate(response_body)


@router.post("/modules/{module_id}/unpublish")
async def unpublish_module(
    request: Request,
    module_id: UUID,
    admin: ModuleAdmin,
    session: Session,
    redis: RedisDep,
    storage: StorageDep,
    idempotency_key: IdempotencyKey,
) -> ModuleResponse:
    async with training_service.storage_effects(storage) as effects:

        async def _execute() -> tuple[int, Any]:
            module = await training_service.unpublish_module(
                session, effects, actor_id=admin.id, module_id=module_id
            )
            return status.HTTP_200_OK, ModuleResponse.from_record(module).model_dump(mode="json")

        _, response_body = await idempotency_service.run_with_idempotency(
            redis,
            owner_id=admin.id,
            method="POST",
            path=request.url.path,
            idempotency_key=idempotency_key,
            handler=_execute,
            commit=effects.wrap_commit(session.commit),
        )
    return ModuleResponse.model_validate(response_body)


@router.delete("/modules/{module_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_module(
    request: Request,
    module_id: UUID,
    admin: ModuleAdmin,
    session: Session,
    redis: RedisDep,
    storage: StorageDep,
    idempotency_key: IdempotencyKey,
) -> None:
    async with training_service.storage_effects(storage) as effects:

        async def _execute() -> tuple[int, Any]:
            await training_service.delete_module(
                session, effects, actor_id=admin.id, module_id=module_id
            )
            return status.HTTP_204_NO_CONTENT, None

        await idempotency_service.run_with_idempotency(
            redis,
            owner_id=admin.id,
            method="DELETE",
            path=request.url.path,
            idempotency_key=idempotency_key,
            handler=_execute,
            commit=effects.wrap_commit(session.commit),
        )


# --- assessments (Step 3) ----------------------------------------------------------------


@router.put("/modules/{module_id}/assessment")
async def put_assessment(
    request: Request,
    module_id: UUID,
    body: AssessmentPutRequest,
    admin: ModuleAdmin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> AdminAssessmentResponse:
    """Create the module's assessment or replace all of its questions and its threshold. Allowed
    after attempts exist: attempts keep the questions and threshold they were graded with."""
    questions = [
        AuthoredQuestion(
            prompt=q.prompt,
            options=tuple(AuthoredOption(o.label, o.is_correct) for o in q.options),
        )
        for q in body.questions
    ]

    async def _execute() -> tuple[int, Any]:
        view = await assessments_service.put_assessment(
            session,
            actor_id=admin.id,
            module_id=module_id,
            threshold_percent=body.threshold_percent,
            questions=questions,
        )
        return status.HTTP_200_OK, AdminAssessmentResponse.from_view(view).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="PUT",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return AdminAssessmentResponse.model_validate(response_body)


@router.get("/modules/{module_id}/assessment")
async def get_assessment(
    module_id: UUID, admin: ModuleAdmin, session: Session
) -> AdminAssessmentResponse:
    return AdminAssessmentResponse.from_view(
        await assessments_service.get_admin_view(session, module_id=module_id)
    )


@router.delete("/modules/{module_id}/assessment", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assessment(
    request: Request,
    module_id: UUID,
    admin: ModuleAdmin,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> None:
    async def _execute() -> tuple[int, Any]:
        await assessments_service.delete_assessment(session, actor_id=admin.id, module_id=module_id)
        return status.HTTP_204_NO_CONTENT, None

    await idempotency_service.run_with_idempotency(
        redis,
        owner_id=admin.id,
        method="DELETE",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )


@router.get("/modules/{module_id}/video")
async def stream_module_video(
    module_id: UUID,
    admin: ModuleAdmin,
    session: Session,
    storage: StorageDep,
    download: Annotated[bool, Query()] = False,
    range: Annotated[str | None, Header()] = None,  # noqa: A002 - the HTTP header's real name
) -> Response:
    """The administrator's preview of a published module's stored video, with HTTP ``Range``
    support. It plays the module's own snapshot through ``serve_module_video`` - the one function
    the employee route (``routers/training.py``) serves it with too."""
    return await serve_module_video(
        session, storage, module_id=module_id, range_header=range, download=download
    )
