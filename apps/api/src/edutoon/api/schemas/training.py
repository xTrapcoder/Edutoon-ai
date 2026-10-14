"""Request/response schemas for ``/v1/admin/training`` (courses and modules).

Status values are ``Literal`` types mirroring the ``CHECK`` constraints of migration 0020, so an
invalid value is a 422 here rather than a constraint violation. Request bodies reject unknown
fields (rule 10). Durations are integer milliseconds (rule 5). A module response describes the
snapshot (size, duration, resolution, frame rate, fingerprint) but never the storage bucket or key:
where the file lives is not something a client needs or should rely on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from edutoon.api.schemas.render import Orientation, Quality
from edutoon.domain.training import (
    DESCRIPTION_MAX_CHARS,
    TITLE_MAX_CHARS,
    CourseStatus,
    ModuleStatus,
)
from edutoon.repositories.training import CourseRecord, EnrolmentRecord, ModuleRecord

Title = Annotated[str, Field(min_length=1, max_length=TITLE_MAX_CHARS)]
Description = Annotated[str, Field(max_length=DESCRIPTION_MAX_CHARS)]


def _not_blank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("must not be blank")
    return value.strip() if value is not None else None


class CourseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    description: Description | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        return _not_blank(value) or value


class CourseUpdateRequest(BaseModel):
    """Only the wording can be edited; ``status`` changes through ``/publish`` and ``/archive``."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    description: Description | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        return _not_blank(value)


class CourseResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    status: CourseStatus
    created_by: UUID
    module_count: int
    published_module_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, course: CourseRecord) -> CourseResponse:
        return cls(
            id=course.id,
            title=course.title,
            description=course.description,
            status=course.status,  # type: ignore[arg-type]
            created_by=course.created_by,
            module_count=course.module_count,
            published_module_count=course.published_module_count,
            created_at=course.created_at,
            updated_at=course.updated_at,
        )


class CourseListResponse(BaseModel):
    items: list[CourseResponse]
    next_cursor: str | None


class RenderSourceRequest(BaseModel):
    """Which finished render to publish - the identifiers the render API already uses."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    script_id: UUID
    orientation: Orientation
    quality: Quality


class ModulePublishRequest(RenderSourceRequest):
    """Publish a render as a new module. ``title`` defaults to the project's title."""

    title: Title | None = None
    description: Description | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        return _not_blank(value)


class ModuleRepublishRequest(RenderSourceRequest):
    """Replace a module's video with a fresh snapshot of this render."""


class ModuleOrderRequest(BaseModel):
    """Every module of the course, once, in the new order."""

    model_config = ConfigDict(extra="forbid")

    module_ids: Annotated[list[UUID], Field(min_length=1, max_length=1200)]

    @field_validator("module_ids")
    @classmethod
    def _no_repeats(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("each module may be listed only once")
        return value


class ModuleResponse(BaseModel):
    id: UUID
    course_id: UUID
    position: int
    title: str
    description: str | None
    status: ModuleStatus
    source_render_output_id: UUID | None
    video_byte_size: int
    video_duration_ms: int
    video_width: int
    video_height: int
    video_fps: int
    video_output_hash: str
    published_by: UUID
    published_at: datetime
    unpublished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, module: ModuleRecord) -> ModuleResponse:
        return cls(
            id=module.id,
            course_id=module.course_id,
            position=module.position,
            title=module.title,
            description=module.description,
            status=module.status,  # type: ignore[arg-type]
            source_render_output_id=module.source_render_output_id,
            video_byte_size=module.video_byte_size,
            video_duration_ms=module.video_duration_ms,
            video_width=module.video_width,
            video_height=module.video_height,
            video_fps=module.video_fps,
            video_output_hash=module.video_output_hash,
            published_by=module.published_by,
            published_at=module.published_at,
            unpublished_at=module.unpublished_at,
            created_at=module.created_at,
            updated_at=module.updated_at,
        )


class ModuleListResponse(BaseModel):
    items: list[ModuleResponse]


CourseStatusFilter = Literal["draft", "published", "archived"]


# --- enrolments --------------------------------------------------------------------------


EnrolmentStatusFilter = Literal["active", "withdrawn"]


class EnrolmentCreateRequest(BaseModel):
    """Enrol this user in the course. If they were enrolled before and withdrawn, that same
    enrolment becomes active again."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID


class EnrolmentResponse(BaseModel):
    id: UUID
    course_id: UUID
    course_title: str
    course_status: CourseStatus
    user_id: UUID
    user_email: str
    user_display_name: str | None
    status: EnrolmentStatusFilter
    assigned_by: UUID
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, enrolment: EnrolmentRecord) -> EnrolmentResponse:
        return cls(
            id=enrolment.id,
            course_id=enrolment.course_id,
            course_title=enrolment.course_title,
            course_status=enrolment.course_status,  # type: ignore[arg-type]
            user_id=enrolment.user_id,
            user_email=enrolment.user_email,
            user_display_name=enrolment.user_display_name,
            status=enrolment.status,  # type: ignore[arg-type]
            assigned_by=enrolment.assigned_by,
            created_at=enrolment.created_at,
            updated_at=enrolment.updated_at,
        )


class EnrolmentListResponse(BaseModel):
    items: list[EnrolmentResponse]
    next_cursor: str | None
