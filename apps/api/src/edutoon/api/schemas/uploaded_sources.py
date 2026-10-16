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

from pydantic import BaseModel, ConfigDict, Field

from edutoon.core.config import get_settings
from edutoon.domain.pasted_text import MAX_CHARS
from edutoon.services.uploaded_sources import UploadedSourceRecord

SourceKind = Literal["pdf", "pptx", "text"]
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
    if record.kind == "pptx":
        return _deck_failure_reason(record)
    if record.kind == "text":
        return "The pasted text could not be used. Paste it again."
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


def _deck_failure_reason(record: UploadedSourceRecord) -> str:
    if record.page_count is None:
        return (
            "This presentation could not be read. It may be damaged or password-protected; "
            "try saving a fresh copy as .pptx."
        )
    if record.page_count > get_settings().MAX_DECK_SLIDES:
        return (
            f"This presentation has {record.page_count} slides; the limit is "
            f"{get_settings().MAX_DECK_SLIDES}. Split it and upload the parts you need."
        )
    return (
        "No text could be read from this presentation - its slides look like pictures only. "
        "Add text, or speaker notes, to the slides and upload it again."
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


class PastedTextRequest(BaseModel):
    """The body of ``POST .../sources/text``: material to build a video from, pasted as text.

    ``title`` names the source in lists (a default is used when it is blank). The bounds on the
    text itself, after cleaning, are ``domain.pasted_text``'s; the ceiling here only stops a body
    far larger than any accepted text from being processed at all.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_CHARS * 2)


class UploadedSourceListResponse(BaseModel):
    items: list[UploadedSourceResponse]
    next_cursor: str | None
