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
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import IdempotencyKey, RedisDep, StorageDep
from edutoon.api.policies import (
    require_admin,
    require_admin_for_course,
    require_admin_for_module,
)
from edutoon.api.schemas.training import (
    CourseCreateRequest,
    CourseListResponse,
    CourseResponse,
    CourseStatusFilter,
    CourseUpdateRequest,
    ModuleListResponse,
    ModuleOrderRequest,
    ModulePublishRequest,
    ModuleRepublishRequest,
    ModuleResponse,
)
from edutoon.core.pagination import DEFAULT_PAGE_SIZE
from edutoon.db.session import get_session
from edutoon.services import idempotency as idempotency_service
from edutoon.services import training as training_service
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
    support. It plays the module's own snapshot, through the same service function the employee
    route will use."""
    opened = await training_service.open_module_video(
        session, storage, module_id=module_id, range_header=range
    )
    if opened.stream is None:
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{opened.unsatisfiable_size}"},
        )
    stream = opened.stream
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(stream.content_length)}
    if stream.content_range:
        headers["Content-Range"] = stream.content_range
    if download:
        headers["Content-Disposition"] = f'attachment; filename="module-{module_id}.mp4"'
    return StreamingResponse(
        stream.iter_chunks(),
        status_code=stream.status_code,
        media_type="video/mp4",
        headers=headers,
    )
