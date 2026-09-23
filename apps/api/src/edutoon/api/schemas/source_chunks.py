"""Response schemas for
``/v1/projects/{project_id}/sources/{source_id}/chunks``.

Read-only: chunks are produced by the parsing pipeline in
``services/uploaded_sources.py``, never created directly by a client.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from edutoon.services.source_chunks import SourceChunkRecord


class SourceChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_id: UUID
    project_id: UUID
    chunk_index: int
    page_from: int | None
    page_to: int | None
    content: str
    token_count: int | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: SourceChunkRecord) -> SourceChunkResponse:
        return cls.model_validate(record)


class SourceChunkListResponse(BaseModel):
    """Not paginated: a source's chunk count is bounded by ``MAX_PDF_PAGES``
    (a few hundred at most), so the full, in-order list is returned at once.
    """

    items: list[SourceChunkResponse]
