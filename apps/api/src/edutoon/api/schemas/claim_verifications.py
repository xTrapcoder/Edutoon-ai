"""Request/response schemas for
``/v1/projects/{project_id}/claim-verifications``.

``status`` is a ``Literal`` mirroring the ``ck_claim_verifications_status``
CHECK constraint in migration 0006 exactly - it is the same set of verdicts
``providers.llm`` produces, restated here as the public API contract.

``VerifyClaimRequest`` forbids unknown fields (rule 10) and rejects a blank
claim (mirroring ``ck_claim_verifications_claim_text_not_blank``); both come
back as a 422 through FastAPI's ``RequestValidationError`` handler.

``ClaimExtractionRequest`` (Phase 6) is the body of ``POST
.../claim-verifications/extraction``, mirroring
``schemas.topics.TopicExtractionRequest`` exactly: an optional ``replace``,
defaulting to ``False``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from edutoon.services.evidence import ClaimCitationRecord, ClaimVerificationRecord

VerdictStatus = Literal["supported", "partially_supported", "unsupported", "contradicted"]


class VerifyClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_text: str

    @field_validator("claim_text")
    @classmethod
    def _claim_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ClaimExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replace: bool = False


class ClaimCitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_chunk_id: UUID
    similarity: float
    created_at: datetime

    @classmethod
    def from_record(cls, record: ClaimCitationRecord) -> ClaimCitationResponse:
        return cls.model_validate(record)


class ClaimVerificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    claim_text: str
    status: VerdictStatus
    rationale: str | None
    embedding_model: str
    llm_model: str
    topic_id: UUID | None
    created_at: datetime
    citations: list[ClaimCitationResponse]

    @classmethod
    def from_records(
        cls,
        verification: ClaimVerificationRecord,
        citations: list[ClaimCitationRecord],
    ) -> ClaimVerificationResponse:
        return cls.model_validate(
            {
                **asdict(verification),
                "citations": [ClaimCitationResponse.from_record(c) for c in citations],
            }
        )


class ClaimVerificationListResponse(BaseModel):
    items: list[ClaimVerificationResponse]
    next_cursor: str | None
