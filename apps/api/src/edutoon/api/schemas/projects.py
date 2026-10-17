"""Request/response schemas for ``/v1/projects``.

``source_type``/``status`` are ``Literal`` types mirroring the ``CHECK``
constraints in migration 0002 exactly, so an invalid value is rejected as a
422 here rather than reaching the database as a raw constraint violation. The
generation settings (``language``, ``orientation``, ``target_duration_ms``) work
the same way against migration 0010, with their vocabulary and limits taken from
:mod:`edutoon.domain.generation_settings`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from edutoon.domain import instructions as instructions_domain
from edutoon.domain.generation_settings import (
    DEFAULT_LANGUAGE,
    DEFAULT_ORIENTATION,
    DEFAULT_TARGET_DURATION_MS,
    MAX_TARGET_DURATION_MS,
    MIN_TARGET_DURATION_MS,
    Language,
    Orientation,
)
from edutoon.domain.project_status import ProjectStatus
from edutoon.services.projects import ProjectRecord

SourceType = Literal["pdf", "topic", "pptx", "text"]

# ``strict`` so only a real integer is a duration: ``"180000"``, ``180000.0`` and
# ``true`` are all refused rather than coerced.
TargetDurationMs = Annotated[
    int, Field(strict=True, ge=MIN_TARGET_DURATION_MS, le=MAX_TARGET_DURATION_MS)
]

# The generation settings are NOT NULL in the schema, so an explicit ``null`` for
# one is rejected on update instead of reaching the database.
_NOT_NULL_FIELDS = ("title", "language", "orientation", "target_duration_ms")


def _not_blank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("must not be blank")
    return value


def _clean_instructions(value: str | None) -> str | None:
    """Instructions are optional: blank is the same as none, and over-long text is a 422."""
    try:
        return instructions_domain.clean(value)
    except instructions_domain.InstructionsError as problem:
        raise ValueError(str(problem)) from problem


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    source_type: SourceType
    description: str | None = None
    language: Language = DEFAULT_LANGUAGE
    orientation: Orientation = DEFAULT_ORIENTATION
    target_duration_ms: TargetDurationMs = DEFAULT_TARGET_DURATION_MS
    # What the owner asks of the lesson, in their own words (``domain.instructions``). Guidance
    # only: it never relaxes a grounding rule.
    instructions: str | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        _not_blank(value)
        return value

    @field_validator("instructions")
    @classmethod
    def _tidy_instructions(cls, value: str | None) -> str | None:
        return _clean_instructions(value)


class ProjectUpdateRequest(BaseModel):
    """All fields optional (PATCH semantics). Only keys actually present in
    the request body reach the service - see ``model_dump(exclude_unset=True)``
    in the router - so omitting a field leaves it untouched, while
    explicitly sending ``null`` for a nullable field (``description``)
    clears it. ``title`` and the generation settings are ``NOT NULL`` in the
    schema, so an explicit ``null`` for any of them is rejected here rather than
    reaching the database.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    language: Language | None = None
    orientation: Orientation | None = None
    target_duration_ms: TargetDurationMs | None = None
    instructions: str | None = None

    @field_validator("title")
    @classmethod
    def _not_blank_when_given(cls, value: str | None) -> str | None:
        _not_blank(value)
        return value

    @field_validator("instructions")
    @classmethod
    def _tidy_instructions(cls, value: str | None) -> str | None:
        return _clean_instructions(value)

    @model_validator(mode="after")
    def _reject_explicit_null_for_not_null_columns(self) -> ProjectUpdateRequest:
        for name in _NOT_NULL_FIELDS:
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null - omit it to leave it unchanged.")
        return self


class InstructionNoteResponse(BaseModel):
    """One thing said back about the owner's instructions: ``applied`` (done in code),
    ``guidance`` (passed to the writers, not guaranteed) or ``unsupported`` (cannot be done)."""

    kind: Literal["applied", "guidance", "unsupported"]
    text: str


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    title: str
    description: str | None
    source_type: SourceType
    status: ProjectStatus
    language: Language
    orientation: Orientation
    target_duration_ms: int
    created_at: datetime
    updated_at: datetime
    instructions: str | None = None
    instructions_notes: list[InstructionNoteResponse] = []

    @classmethod
    def from_record(cls, record: ProjectRecord) -> ProjectResponse:
        response = cls.model_validate(record)
        if record.instructions:
            understood = instructions_domain.interpret(record.instructions)
            response.instructions_notes = [
                InstructionNoteResponse(kind=note.kind, text=note.text)
                for note in instructions_domain.notes(record.instructions, understood)
            ]
        return response


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    next_cursor: str | None
