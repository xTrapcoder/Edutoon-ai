"""Request/response schemas for ``/v1/projects/{project_id}/scripts``.

``status`` is a ``Literal`` mirroring the ``ck_scripts_status`` CHECK
constraint in migration 0007 exactly - the same status machine the
repository and service layers drive (``pending -> draft -> approved``, plus
``failed`` / ``superseded``).

``ScriptGenerateRequest`` carries no fields yet: generation takes its
parameters from configuration, not the caller. It exists so an unknown field
in the request body is still rejected with a 422 (rule 10) rather than
silently ignored, and as the extension point for future per-run options.

The per-segment ``claims`` on a script's detail view are the traceability
link back to the Evidence Engine: each is the verified claim a segment's
narration was allowed to draw on. ``VerdictStatus`` is imported from the
claim-verifications schema so the two surfaces cannot drift.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from edutoon.api.schemas.claim_verifications import VerdictStatus
from edutoon.repositories.scripts import ScriptRecord
from edutoon.services.scripts import ScriptDetail

ScriptStatus = Literal["pending", "draft", "approved", "failed", "superseded"]


class ScriptGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    status: ScriptStatus
    llm_model: str
    estimated_duration_ms: int | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: ScriptRecord) -> ScriptResponse:
        return cls.model_validate(record)


class ScriptListResponse(BaseModel):
    items: list[ScriptResponse]
    next_cursor: str | None


class CitedClaimResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    claim_text: str
    status: VerdictStatus
    rationale: str | None
    created_at: datetime


class ScriptSegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    segment_index: int
    narration_text: str
    estimated_duration_ms: int
    topic_id: UUID | None
    created_at: datetime
    claims: list[CitedClaimResponse]


class ScriptDetailResponse(ScriptResponse):
    segments: list[ScriptSegmentResponse]

    @classmethod
    def from_detail(cls, detail: ScriptDetail) -> ScriptDetailResponse:
        return cls.model_validate(
            {
                **asdict(detail.script),
                "segments": [
                    {
                        **asdict(segment_detail.segment),
                        "claims": [
                            CitedClaimResponse.model_validate(claim)
                            for claim in segment_detail.claims
                        ],
                    }
                    for segment_detail in detail.segments
                ],
            }
        )
