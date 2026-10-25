"""``/v1/projects/{project_id}/scripts/{script_id}`` - Phase 8's narration
synthesis API surface: enqueue, status/progress, and segment-audio streaming
playback.

Synthesis is asynchronous, the same shape as script generation (SG-5):
``POST /synthesis`` enqueues a ``synthesise_voice`` job and returns ``202``
with it; the caller polls ``GET /synthesis`` for progress and, once each
segment is ``ready``, plays it from ``GET /segments/{segment_id}/audio``.

``project_id`` ownership is checked first on every route (rule 9: a project
the caller doesn't own is a 404, never a 403), then the script's own
ownership, and - for the audio route - the segment's own. The enqueue route
requires an ``Idempotency-Key`` (rule 8) and dedupes through
``services/idempotency.py`` exactly like ``routers/scripts.py``; its
transaction is committed before its result is cached (``commit=session.commit``).

The audio route supports HTTP ``Range`` requests (browser audio seeking):
the client's ``Range`` header, if given, is passed straight through to
object storage, which does the actual byte-range arithmetic; this route only
mirrors back what storage returned, including the ``206``/``200`` status.

Rule 2 holds here as everywhere: this module reaches storage only through the
``api.dependencies`` injection seam and all persistence goes through
``services.script_audio`` - never a repository or provider import.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import IdempotencyKey, RedisDep, StorageDep, get_current_user
from edutoon.api.policies import require_admin_for_project
from edutoon.api.schemas.jobs import JobResponse
from edutoon.api.schemas.script_audio import SynthesisRequest, SynthesisStatusResponse
from edutoon.db.session import get_session
from edutoon.services import idempotency as idempotency_service
from edutoon.services import script_audio as script_audio_service
from edutoon.services.users import UserRecord

router = APIRouter(
    prefix="/projects/{project_id}/scripts/{script_id}",
    tags=["synthesis"],
    dependencies=[Depends(require_admin_for_project)],
)

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/synthesis", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_synthesis(
    request: Request,
    project_id: UUID,
    script_id: UUID,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
    body: SynthesisRequest | None = None,
) -> JobResponse:
    async def _execute() -> tuple[int, Any]:
        result = await script_audio_service.enqueue_synthesis(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            script_id=script_id,
            idempotency_key=idempotency_key,
        )
        return status.HTTP_202_ACCEPTED, JobResponse.from_record(result.job).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return JobResponse.model_validate(response_body)


@router.get("/synthesis")
async def get_synthesis_status(
    project_id: UUID,
    script_id: UUID,
    current_user: CurrentUser,
    session: Session,
) -> SynthesisStatusResponse:
    result = await script_audio_service.get_synthesis_status(
        session, project_id=project_id, owner_id=current_user.id, script_id=script_id
    )
    return SynthesisStatusResponse.from_status(result)


@router.get("/segments/{segment_id}/audio")
async def stream_segment_audio(
    project_id: UUID,
    script_id: UUID,
    segment_id: UUID,
    current_user: CurrentUser,
    session: Session,
    storage: StorageDep,
    range: Annotated[str | None, Header()] = None,  # noqa: A002 - the HTTP header's real name
) -> Response:
    data, audio = await script_audio_service.get_segment_audio_for_playback(
        session,
        storage,
        project_id=project_id,
        owner_id=current_user.id,
        script_id=script_id,
        segment_id=segment_id,
        range_header=range,
    )
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(data.content_length)}
    if data.content_range:
        headers["Content-Range"] = data.content_range
    return Response(
        content=data.body,
        status_code=data.status_code,
        media_type=data.content_type,
        headers=headers,
    )
