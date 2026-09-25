"""Request/response schemas for ``/v1/projects/{project_id}/topics``.

``origin`` and ``status`` are the domain's ``Literal`` types, which mirror the
``ck_project_topics_origin`` / ``ck_project_topics_status`` CHECK constraints
exactly, so an invalid ``status`` is rejected as a 422 here rather than reaching
the database.

Every request model forbids unknown fields (rule 10). The *limits* on what a user
may type (keyword count and length, title and summary length) live in settings
and are enforced by the service with a 400 and a message that names the limit;
the bounds below exist only so an absurd body is refused before any work is done,
and are deliberately far above any sensible setting.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from edutoon.domain.topic import TopicOrigin, TopicStatus
from edutoon.services.project_topics import TopicWithEvidence

# Request-size guards, not product limits (those are settings).
_MAX_KEYWORDS_PER_REQUEST = 200
_MAX_TEXT_CHARS = 2_000

_Text = Annotated[str, StringConstraints(max_length=_MAX_TEXT_CHARS)]


class TopicKeywordsRequest(BaseModel):
    """The user's own topics, in the order they should appear."""

    model_config = ConfigDict(extra="forbid")

    keywords: Annotated[list[_Text], Field(min_length=1, max_length=_MAX_KEYWORDS_PER_REQUEST)]


class TopicExtractionRequest(BaseModel):
    """``replace`` - overwrite the topics a previous extraction derived (the
    user's own are always kept). ``False`` refuses to run once there are any.
    """

    model_config = ConfigDict(extra="forbid")

    replace: bool = False


class TopicUpdateRequest(BaseModel):
    """PATCH semantics: only the keys present change. ``summary`` may be
    ``null`` to clear it; ``status`` and ``title`` are ``NOT NULL`` in the
    schema, so an explicit ``null`` for either is rejected here.
    """

    model_config = ConfigDict(extra="forbid")

    status: TopicStatus | None = None
    title: _Text | None = None
    summary: _Text | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> TopicUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("Give at least one of: status, title, summary.")
        for name in ("status", "title"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null - omit it to leave it unchanged.")
        return self


class TopicEvidenceResponse(BaseModel):
    """Where a topic was drawn from: one page-range of one uploaded source."""

    model_config = ConfigDict(from_attributes=True)

    source_chunk_id: UUID
    source_id: UUID
    page_from: int | None
    page_to: int | None


class TopicResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    parent_id: UUID | None
    title: str
    summary: str | None
    keywords: list[str]
    position: int
    depth: int
    status: TopicStatus
    origin: TopicOrigin
    evidence: list[TopicEvidenceResponse]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_outline_item(cls, item: TopicWithEvidence) -> TopicResponse:
        # Validated, not constructed: ``status`` / ``origin`` are plain strings on
        # the stored record and pydantic checks them against the ``Literal``s.
        return cls.model_validate(
            {
                **asdict(item.topic),
                "evidence": [asdict(record) for record in item.evidence],
            }
        )


class TopicListResponse(BaseModel):
    """Topics in reading order (depth-first, siblings by position). Not
    paginated - see ``services.project_topics.list_topic_outline``.
    """

    items: list[TopicResponse]

    @classmethod
    def from_outline(cls, outline: list[TopicWithEvidence]) -> TopicListResponse:
        return cls(items=[TopicResponse.from_outline_item(item) for item in outline])
