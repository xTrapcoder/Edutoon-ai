"""``/v1/projects/{project_id}/claim-verifications`` - synchronous claim
verification against a project's indexed source material.

``project_id`` ownership is checked first on every route (rule 9: a project
the caller doesn't own is a 404, never a 403). The POST requires an
``Idempotency-Key`` (rule 8) and dedupes through ``services/idempotency.py``
exactly like ``routers/projects.py``: a repeat of the same
(caller, method, path, key) replays the original verdict instead of
re-running the (paid) embedding + LLM round trip.

Rule 2 holds here as everywhere: this module reaches Redis, the embeddings
provider and the LLM only through the ``api.dependencies`` injection seam,
and all persistence goes through ``services.evidence`` - never a repository
or provider import.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import EmbeddingsDep, LLMDep, RedisDep, get_current_user
from edutoon.api.schemas.claim_verifications import (
    ClaimVerificationListResponse,
    ClaimVerificationResponse,
    VerifyClaimRequest,
)
from edutoon.core.pagination import DEFAULT_PAGE_SIZE
from edutoon.db.session import get_session
from edutoon.services import evidence as evidence_service
from edutoon.services import idempotency as idempotency_service
from edutoon.services import projects as projects_service
from edutoon.services.users import UserRecord

router = APIRouter(
    prefix="/projects/{project_id}/claim-verifications", tags=["claim-verifications"]
)

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]
IdempotencyKey = Annotated[str, Header()]


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
    )
    return ClaimVerificationResponse.model_validate(response_body)


@router.get("")
async def list_claim_verifications(
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> ClaimVerificationListResponse:
    await projects_service.get_project(
        session, project_id=project_id, owner_id=current_user.id
    )

    page = await evidence_service.list_claim_verifications(
        session, project_id=project_id, limit=limit, cursor=cursor
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
