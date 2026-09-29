"""Request/response schemas for
``/v1/projects/{project_id}/scripts/{script_id}/storyboard`` (Phase 9).

``Template`` mirrors the ``ck_script_segment_scenes_template`` CHECK
constraint (migration 0015) exactly, the same pattern as ``AudioStatus`` /
``ScriptStatus``. ``content`` stays a loosely-typed object - its real shape
varies by template and is enforced by ``domain.storyboard_template``, not
by this schema (the same "structure here, semantics in the domain" split
every other generated-content response in this API already follows).

Completeness (``is_complete`` / per-segment ``is_ready`` / ``is_stale``) and
the reviewer flag are computed fresh by ``services.storyboard`` on every
read - never cached here, never copied from a job's own historical result -
so the UI can never show a finished-looking timeline that is actually
missing sections.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from edutoon.api.schemas.jobs import JobResponse
from edutoon.api.schemas.scripts import ScriptResponse
from edutoon.services.storyboard import SegmentStoryboardDetail, StoryboardStatus

Template = Literal[
    "title_card",
    "bullet_points",
    "labelled_diagram",
    "split_text_illustration",
    "full_bleed_illustration",
    "formula_or_code",
]


class StoryboardEnqueueRequest(BaseModel):
    """Carries no fields yet: generation takes its settings from
    configuration, not the caller. Exists so an unknown field in the
    request body is rejected with 422 (rule 10), the same pattern as
    ``SynthesisRequest``.
    """

    model_config = ConfigDict(extra="forbid")


class StoryboardEnqueueResponse(BaseModel):
    job: JobResponse
    estimated_provider_calls: int


class SceneResponse(BaseModel):
    id: UUID
    scene_index: int
    # Computed on read from the current narration - never the stored value (see
    # ``StoryboardStatusResponse.timings_authoritative`` for when it may still move).
    start_ms: int
    duration_ms: int
    template: Template
    content: dict[str, Any]
    # The soft provenance signal (spec section 14) - a reviewer flag, never
    # a rejection; recomputed fresh on every read.
    reviewer_flag: bool
    updated_at: datetime


class SegmentStoryboardResponse(BaseModel):
    id: UUID
    segment_index: int
    expected_scene_count: int
    audio_measured_duration_ms: int | None
    is_stale: bool
    is_ready: bool
    scenes: list[SceneResponse]

    @classmethod
    def from_detail(cls, detail: SegmentStoryboardDetail) -> SegmentStoryboardResponse:
        return cls(
            id=detail.segment.id,
            segment_index=detail.segment.segment_index,
            expected_scene_count=detail.expected_scene_count,
            audio_measured_duration_ms=detail.audio.measured_duration_ms if detail.audio else None,
            is_stale=detail.is_stale,
            is_ready=detail.is_ready,
            scenes=[
                SceneResponse(
                    id=scene.scene.id,
                    scene_index=scene.scene.scene_index,
                    start_ms=scene.start_ms,
                    duration_ms=scene.scene.duration_ms,
                    template=scene.scene.template,  # type: ignore[arg-type]
                    content=scene.scene.content,
                    reviewer_flag=scene.reviewer_flag,
                    updated_at=scene.scene.updated_at,
                )
                for scene in detail.scenes
            ],
        )


class StoryboardStatusResponse(BaseModel):
    script: ScriptResponse
    job: JobResponse | None
    segments: list[SegmentStoryboardResponse]
    ready_segment_count: int
    not_ready_segment_ids: list[UUID]
    is_complete: bool
    # ``False`` while narration is stale, being (re)made or failed: the ``start_ms`` values are
    # then a best estimate that may move once narration finishes. ``True`` means every timing
    # input is final.
    timings_authoritative: bool
    # Current scenes whose model-written visual was invalid and dropped: they show text instead.
    # Always counted fresh from the scenes, so a reviewer is told until they are regenerated.
    dropped_visual_count: int
    estimated_provider_calls: int
    actual_provider_calls: int | None

    @classmethod
    def from_status(cls, status: StoryboardStatus) -> StoryboardStatusResponse:
        return cls(
            script=ScriptResponse.from_record(status.script),
            job=JobResponse.from_record(status.job) if status.job else None,
            segments=[SegmentStoryboardResponse.from_detail(detail) for detail in status.segments],
            ready_segment_count=status.ready_segment_count,
            not_ready_segment_ids=list(status.not_ready_segment_ids),
            is_complete=status.is_complete,
            timings_authoritative=status.timings_authoritative,
            dropped_visual_count=status.dropped_visual_count,
            estimated_provider_calls=status.estimated_provider_calls,
            actual_provider_calls=status.actual_provider_calls,
        )
