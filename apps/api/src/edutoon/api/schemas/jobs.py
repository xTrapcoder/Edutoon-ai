"""Request/response schemas for ``/v1/projects/{project_id}/jobs``.

``kind``/``status`` are ``Literal`` types mirroring the ``ck_jobs_kind`` /
``ck_jobs_status`` CHECK constraints (migration 0003, ``kind`` widened by
0012 and 0015) exactly, the same pattern as ``UploadedSourceResponse`` and
``ScriptResponse``. Read-only: there is no request schema because this
router has no mutating routes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from edutoon.repositories.jobs import JobRecord

JobKind = Literal[
    "parse_source",
    "extract_topics",
    "extract_claims",
    "build_evidence",
    "generate_script",
    "synthesise_voice",
    "generate_storyboard",
    "render_video",
]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    kind: JobKind
    status: JobStatus
    priority: int
    attempts: int
    max_attempts: int
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    scheduled_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: JobRecord) -> JobResponse:
        return cls.model_validate(record)


class JobListResponse(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None
