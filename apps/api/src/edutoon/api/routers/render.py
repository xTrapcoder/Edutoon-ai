"""Phase 10's video-rendering API surface.

Asynchronous, the same shape as narration and storyboard generation: ``POST .../render``
queues a ``render_video`` job and answers ``202`` with it and an estimate of the work; the
caller polls ``GET .../render/progress`` (one cheap query) while it runs and reads
``GET .../render`` (the full per-scene status, computed fresh from fingerprints) when it
changes state. ``GET .../render/video`` streams the finished MP4 with HTTP ``Range`` support,
so a browser can play, pause and seek - and download it - without the server ever holding the
video in memory. ``POST /render/retry`` repeats the last failed render; finished chunks are
reused, so it does only what was left.

``project_id`` ownership is checked first on every route (rule 9: a project the caller doesn't
own is a 404, never a 403). Both mutating routes require an ``Idempotency-Key`` (rule 8) and
dedupe through ``services/idempotency.py`` exactly like ``routers/storyboard.py``; a render
already queued or running for the project is a ``409``.

Rule 2 holds here as everywhere: everything goes through ``services.render``.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import IdempotencyKey, RedisDep, StorageDep, get_current_user
from edutoon.api.schemas.jobs import JobResponse
from edutoon.api.schemas.render import (
    Orientation,
    Quality,
    RenderEnqueueRequest,
    RenderEnqueueResponse,
    RenderProgressResponse,
    RenderProgressStatusResponse,
    RenderRetryRequest,
    RenderStatusResponse,
)
from edutoon.db.session import get_session
from edutoon.services import idempotency as idempotency_service
from edutoon.services import render as render_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/projects/{project_id}", tags=["render"])

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/scripts/{script_id}/render", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_render(
    request: Request,
    project_id: UUID,
    script_id: UUID,
    body: RenderEnqueueRequest,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> RenderEnqueueResponse:
    async def _execute() -> tuple[int, Any]:
        result = await render_service.enqueue_render(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            script_id=script_id,
            orientation=body.orientation,
            quality=body.quality,
            idempotency_key=idempotency_key,
        )
        response = RenderEnqueueResponse.from_enqueued(result)
        return status.HTTP_202_ACCEPTED, response.model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return RenderEnqueueResponse.model_validate(response_body)


@router.post("/render/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_render(
    request: Request,
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
    body: RenderRetryRequest | None = None,
) -> RenderEnqueueResponse:
    async def _execute() -> tuple[int, Any]:
        result = await render_service.retry_render(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            idempotency_key=idempotency_key,
        )
        response = RenderEnqueueResponse.from_enqueued(result)
        return status.HTTP_202_ACCEPTED, response.model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return RenderEnqueueResponse.model_validate(response_body)


@router.get("/render/progress")
async def get_render_progress(
    project_id: UUID, current_user: CurrentUser, session: Session
) -> RenderProgressStatusResponse:
    job = await render_service.get_render_progress(
        session, project_id=project_id, owner_id=current_user.id
    )
    return RenderProgressStatusResponse(
        job=JobResponse.from_record(job) if job else None,
        progress=RenderProgressResponse.from_job(job),
    )


@router.get("/scripts/{script_id}/render")
async def get_render(
    project_id: UUID,
    script_id: UUID,
    orientation: Orientation,
    quality: Quality,
    current_user: CurrentUser,
    session: Session,
) -> RenderStatusResponse:
    result = await render_service.get_render_status(
        session,
        project_id=project_id,
        owner_id=current_user.id,
        script_id=script_id,
        orientation=orientation,
        quality=quality,
    )
    return RenderStatusResponse.from_status(result)


@router.get("/scripts/{script_id}/render/video")
async def stream_render_video(
    project_id: UUID,
    script_id: UUID,
    orientation: Orientation,
    quality: Quality,
    current_user: CurrentUser,
    session: Session,
    storage: StorageDep,
    download: Annotated[bool, Query()] = False,
    range: Annotated[str | None, Header()] = None,  # noqa: A002 - the HTTP header's real name
) -> Response:
    opened = await render_service.open_render_output(
        session,
        storage,
        project_id=project_id,
        owner_id=current_user.id,
        script_id=script_id,
        orientation=orientation,
        quality=quality,
        range_header=range,
    )
    if opened.stream is None:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{opened.unsatisfiable_size}"},
        )
    stream = opened.stream
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(stream.content_length)}
    if stream.content_range:
        headers["Content-Range"] = stream.content_range
    if download:
        headers["Content-Disposition"] = f'attachment; filename="video-{orientation}-{quality}.mp4"'
    return StreamingResponse(
        stream.iter_chunks(),
        status_code=stream.status_code,
        media_type="video/mp4",
        headers=headers,
    )
