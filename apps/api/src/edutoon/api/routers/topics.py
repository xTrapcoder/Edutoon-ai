"""``/v1/projects/{project_id}/topics`` - topic understanding (Phase 5).

Four routes, in the order a user meets them:

- ``POST /topics`` - store the user's own keywords as accepted topics (``201``).
  Instant and free: no model is involved.
- ``POST /topics/extraction`` - enqueue an ``extract_topics`` job (``202`` with
  the job). The ``worker`` process runs it; poll ``GET /jobs`` for its result,
  which says how much of the material the model saw (``truncated``) and what it
  made. Refused with ``409`` while one is already queued or running, and - unless
  ``replace`` is true - once topics have been extracted.
- ``GET /topics`` - the outline, in reading order, each topic with the source
  pages it cites.
- ``PATCH /topics/{topic_id}`` - the review step: accept or reject a topic and
  correct its wording.

``project_id`` ownership is checked first on every route (rule 9: a project the
caller does not own is a 404, never a 403), and again for the nested
``topic_id``. Every mutating route requires an ``Idempotency-Key`` (rule 8) and
dedupes through ``services/idempotency.py`` exactly like ``routers/scripts.py``,
and every request body forbids unknown fields (rule 10).

Rule 2 holds here as everywhere: Redis is reached only through the
``api.dependencies`` seam and all persistence goes through the services - never
a repository or provider import.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import RedisDep, get_current_user
from edutoon.api.schemas.jobs import JobResponse
from edutoon.api.schemas.topics import (
    TopicExtractionRequest,
    TopicKeywordsRequest,
    TopicListResponse,
    TopicResponse,
    TopicUpdateRequest,
)
from edutoon.db.session import get_session
from edutoon.services import idempotency as idempotency_service
from edutoon.services import project_topics as project_topics_service
from edutoon.services import topic_extraction as topic_extraction_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/projects/{project_id}/topics", tags=["topics"])

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]
IdempotencyKey = Annotated[str, Header()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_keywords(
    request: Request,
    project_id: UUID,
    body: TopicKeywordsRequest,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> TopicListResponse:
    async def _execute() -> tuple[int, Any]:
        created = await project_topics_service.add_keyword_topics(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            keywords=body.keywords,
        )
        return status.HTTP_201_CREATED, TopicListResponse.from_outline(created).model_dump(
            mode="json"
        )

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
    )
    return TopicListResponse.model_validate(response_body)


@router.post("/extraction", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_extraction(
    request: Request,
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
    body: TopicExtractionRequest | None = None,
) -> JobResponse:
    replace = body.replace if body is not None else False

    async def _execute() -> tuple[int, Any]:
        # Ownership, the active-job guard, the "has something to extract from"
        # check, the replace rules and the job insert all live inside the
        # service, so running it inside the idempotency-guarded body means only
        # the lock winner ever touches the session.
        result = await topic_extraction_service.enqueue_topic_extraction(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            idempotency_key=idempotency_key,
            replace=replace,
        )
        return status.HTTP_202_ACCEPTED, JobResponse.from_record(result.job).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
    )
    return JobResponse.model_validate(response_body)


@router.get("")
async def list_topics(
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
) -> TopicListResponse:
    outline = await project_topics_service.list_topic_outline(
        session, project_id=project_id, owner_id=current_user.id
    )
    return TopicListResponse.from_outline(outline)


@router.patch("/{topic_id}")
async def update_topic(
    request: Request,
    project_id: UUID,
    topic_id: UUID,
    body: TopicUpdateRequest,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
) -> TopicResponse:
    fields = body.model_dump(exclude_unset=True)

    async def _execute() -> tuple[int, Any]:
        updated = await project_topics_service.update_topic(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            topic_id=topic_id,
            fields=fields,
        )
        return status.HTTP_200_OK, TopicResponse.from_outline_item(updated).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="PATCH",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
    )
    return TopicResponse.model_validate(response_body)
