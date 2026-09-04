"""Request/response schemas for ``/v1/projects``.

``source_type``/``status`` are ``Literal`` types mirroring the ``CHECK``
constraints in migration 0002 exactly, so an invalid value is rejected as a
422 here rather than reaching the database as a raw constraint violation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from edutoon.services.projects import ProjectRecord

SourceType = Literal["pdf", "topic"]
ProjectStatus = Literal["draft", "processing", "ready", "failed", "archived"]


def _not_blank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("must not be blank")
    return value


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    source_type: SourceType
    description: str | None = None
    language: str = "en-GB"

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        _not_blank(value)
        return value


class ProjectUpdateRequest(BaseModel):
    """All fields optional (PATCH semantics). Only keys actually present in
    the request body reach the service - see ``model_dump(exclude_unset=True)``
    in the router - so omitting a field leaves it untouched, while
    explicitly sending ``null`` for a nullable field (``description``)
    clears it. ``title``/``language`` are ``NOT NULL`` in the schema, so an
    explicit ``null`` for either is rejected here rather than reaching the
    database.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    language: str | None = None

    @field_validator("title", "language")
    @classmethod
    def _not_blank_when_given(cls, value: str | None) -> str | None:
        _not_blank(value)
        return value

    @model_validator(mode="after")
    def _reject_explicit_null_for_not_null_columns(self) -> ProjectUpdateRequest:
        for name in ("title", "language"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null - omit it to leave it unchanged.")
        return self


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    title: str
    description: str | None
    source_type: SourceType
    status: ProjectStatus
    language: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: ProjectRecord) -> ProjectResponse:
        return cls.model_validate(record)


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    next_cursor: str | None
