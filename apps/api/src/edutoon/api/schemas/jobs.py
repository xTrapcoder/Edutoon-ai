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

from edutoon.domain.llm_usage import LLM_CALL_KINDS, UsageEntry, UsageReport
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


class UsageEntryResponse(BaseModel):
    """Language-model usage for one kind of call, or in total. A token figure is ``null`` when the
    provider reported none - unavailable, never guessed as zero; ``calls_with_usage`` below
    ``calls`` means the sums are a lower bound."""

    calls: int
    calls_with_usage: int
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None

    @classmethod
    def from_entry(cls, entry: UsageEntry) -> UsageEntryResponse:
        return cls(**entry.as_dict())  # type: ignore[arg-type]


class ProjectUsageResponse(BaseModel):
    """What the language model has cost a project, by kind of call. ``by_call`` holds only the
    kinds that were used; the keys are ``topic_extraction``, ``claim_extraction``,
    ``claim_judge``, ``script_outline``, ``script_section``, ``script_correction`` and
    ``storyboard``."""

    total: UsageEntryResponse
    by_call: dict[str, UsageEntryResponse]

    @classmethod
    def from_report(cls, report: UsageReport) -> ProjectUsageResponse:
        return cls(
            total=UsageEntryResponse.from_entry(report.total),
            by_call={
                kind: UsageEntryResponse.from_entry(report.entry(kind))
                for kind in LLM_CALL_KINDS
                if report.entry(kind).calls
            },
        )


class JobListResponse(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None
