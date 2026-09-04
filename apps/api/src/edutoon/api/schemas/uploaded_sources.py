"""Request/response schemas for ``/v1/projects/{project_id}/sources``.

There is no Pydantic request model here: the endpoint accepts
``multipart/form-data`` (a raw file), which FastAPI can't validate
declaratively the way it does a JSON body. The router enforces the
equivalent contract by hand - only ``application/pdf``, a non-blank
filename, a `%PDF` magic-byte check, a size ceiling, and rejection of any
form field beyond ``file`` (rule 10).

``kind``/``status`` are ``Literal`` types mirroring the ``CHECK``
constraints in migration 0004 exactly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from edutoon.services.uploaded_sources import UploadedSourceRecord

SourceKind = Literal["pdf"]
SourceStatus = Literal["pending", "processing", "parsed", "failed"]


class UploadedSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    uploader_id: UUID | None
    kind: SourceKind
    original_filename: str
    content_type: str
    byte_size: int
    checksum_sha256: str
    page_count: int | None
    status: SourceStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: UploadedSourceRecord) -> UploadedSourceResponse:
        return cls.model_validate(record)


class UploadedSourceListResponse(BaseModel):
    items: list[UploadedSourceResponse]
    next_cursor: str | None
