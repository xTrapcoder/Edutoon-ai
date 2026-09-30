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

from edutoon.core.config import get_settings
from edutoon.services.uploaded_sources import UploadedSourceRecord

SourceKind = Literal["pdf"]
SourceStatus = Literal["pending", "processing", "parsed", "failed"]


def failure_reason(record: UploadedSourceRecord, *, max_pages: int) -> str | None:
    """Why a source failed, in words for its owner, or ``None`` if it did not.

    Derived, not stored: every way a source can fail leaves a distinct footprint - no page count
    (the file could not be read), a page count over the limit (too long), or a page count within
    it (readable, but no text could be taken from it) - so nothing new is persisted and the
    reason is the same on every read.
    """
    if record.status != "failed":
        return None
    if record.page_count is None:
        return (
            "This PDF could not be read. It may be damaged or password-protected; "
            "try saving a fresh copy."
        )
    if record.page_count > max_pages:
        return (
            f"This PDF has {record.page_count} pages; the limit is {max_pages}. "
            "Split it and upload the parts you need."
        )
    return (
        "No text could be read from this PDF - it looks like a scan or a set of images. "
        "Upload a text-based PDF, or run it through text recognition (OCR) first."
    )


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
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: UploadedSourceRecord) -> UploadedSourceResponse:
        response = cls.model_validate(record)
        response.failure_reason = failure_reason(record, max_pages=get_settings().MAX_PDF_PAGES)
        return response


class UploadedSourceListResponse(BaseModel):
    items: list[UploadedSourceResponse]
    next_cursor: str | None
