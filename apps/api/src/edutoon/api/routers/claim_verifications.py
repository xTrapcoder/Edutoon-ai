"""``/v1/projects/{project_id}/claim-verifications`` - synchronous claim
verification against a project's indexed source material, plus (Phase 6)
turning a project's accepted topics into candidate claims and verifying
each one the same way.

``project_id`` ownership is checked first on every route (rule 9: a project
the caller doesn't own is a 404, never a 403). Every mutating route requires
an ``Idempotency-Key`` (rule 8; one to 255 characters, otherwise 422 - before
any paid call is made) and dedupes through ``services/idempotency.py`` exactly
like ``routers/projects.py``: a repeat of the same (caller, method, path, key)
replays the original result instead of re-running paid work. Each handler's
transaction is committed before its result is cached and returned
(``commit=session.commit``); the session dependency would otherwise commit
only after the response has been sent, and a failed commit would leave a
cached result that was never stored.

Rule 2 holds here as everywhere: this module reaches Redis, the embeddings
provider and the LLM only through the ``api.dependencies`` injection seam,
and all persistence goes through ``services.evidence`` / ``services.claim_extraction``
- never a repository or provider import.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import (
    EmbeddingsDep,
    IdempotencyKey,
    LLMDep,
    RedisDep,
    get_current_user,
)
from edutoon.api.schemas.claim_verifications import (
    ClaimExtractionRequest,
    ClaimVerificationListResponse,
    ClaimVerificationResponse,
    VerifyClaimRequest,
)
from edutoon.api.schemas.jobs import JobResponse
from edutoon.core.pagination import DEFAULT_PAGE_SIZE
from edutoon.db.session import get_session
from edutoon.services import claim_extraction as claim_extraction_service
from edutoon.services import evidence as evidence_service
from edutoon.services import idempotency as idempotency_service
from edutoon.services import projects as projects_service
from edutoon.services.users import UserRecord

router = APIRouter(
    prefix="/projects/{project_id}/claim-verifications", tags=["claim-verifications"]
)

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_claim_verification(
    request: Request,
    project_id: UUID,
    body: VerifyClaimRequest,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    embeddings: EmbeddingsDep,
    llm: LLMDep,
    idempotency_key: IdempotencyKey,
) -> ClaimVerificationResponse:
    async def _execute() -> tuple[int, Any]:
        # Ownership check lives inside the idempotency-guarded body (rule 9:
        # a non-owned project is a 404): only the lock winner ever touches
        # the session, so a genuine concurrent duplicate is a clean 409
        # rather than two coroutines racing on the same connection.
        await projects_service.get_project(
            session, project_id=project_id, owner_id=current_user.id
        )
        verification = await evidence_service.verify_claim(
            session,
            embeddings,
            llm,
            project_id=project_id,
            claim_text=body.claim_text,
        )
        citations = await evidence_service.list_claim_citations(session, verification.id)
        return status.HTTP_201_CREATED, ClaimVerificationResponse.from_records(
            verification, citations
        ).model_dump(mode="json")

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
        commit=session.commit,
    )
    return ClaimVerificationResponse.model_validate(response_body)


@router.post("/extraction", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_claim_extraction(
    request: Request,
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    idempotency_key: IdempotencyKey,
    body: ClaimExtractionRequest | None = None,
) -> JobResponse:
    """Turn the project's accepted topics into candidate claims and verify
    each one (Phase 6). ``202`` with the queued ``extract_claims`` job;
    progress and the result are on ``GET /v1/projects/{id}/jobs``. Refused
    with ``409`` while one is already queued or running for this project;
    otherwise always accepted - extraction is per-topic and additive, so a
    repeat call after accepting more topics is the ordinary way to cover them
    (``services.claim_extraction`` decides, topic by topic, what is already
    covered; ``replace`` re-runs every accepted topic instead of only new
    ones).
    """
    replace = body.replace if body is not None else False

    async def _execute() -> tuple[int, Any]:
        # Ownership, the active-job guard and the "at least one accepted
        # topic" check all live inside the service, so running it inside the
        # idempotency-guarded body means only the lock winner ever touches
        # the session.
        result = await claim_extraction_service.enqueue_claim_extraction(
            session,
            project_id=project_id,
            owner_id=current_user.id,
            idempotency_key=idempotency_key,
            replace=replace,
        )
        return status.HTTP_202_ACCEPTED, JobResponse.from_record(result.job).model_dump(
            mode="json"
        )

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


@router.get("")
async def list_claim_verifications(
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    topic_id: UUID | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> ClaimVerificationListResponse:
    """The project's claim verifications, newest first. ``topic_id`` narrows
    the page to claims extracted for that one topic (Phase 6); omitted, every
    verification is returned, manual and extracted alike.
    """
    await projects_service.get_project(
        session, project_id=project_id, owner_id=current_user.id
    )

    page = await evidence_service.list_claim_verifications(
        session, project_id=project_id, topic_id=topic_id, limit=limit, cursor=cursor
    )
    citations_by_verification = await evidence_service.list_claim_citations_for(
        session, [verification.id for verification in page.items]
    )
    return ClaimVerificationListResponse(
        items=[
            ClaimVerificationResponse.from_records(
                verification, citations_by_verification.get(verification.id, [])
            )
            for verification in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/{verification_id}")
async def get_claim_verification(
    project_id: UUID,
    verification_id: UUID,
    current_user: CurrentUser,
    session: Session,
) -> ClaimVerificationResponse:
    await projects_service.get_project(
        session, project_id=project_id, owner_id=current_user.id
    )
    verification = await evidence_service.get_claim_verification(
        session, verification_id=verification_id, project_id=project_id
    )
    citations = await evidence_service.list_claim_citations(session, verification.id)
    return ClaimVerificationResponse.from_records(verification, citations)
