"""Request/response schemas for the Phase 10 render endpoints.

``Orientation`` / ``Quality`` / ``SceneState`` mirror the domain vocabularies and the
``ck_render_*`` CHECK constraints (migration 0016), the same pattern as ``Template`` in the
storyboard schemas. Completeness (``is_complete``, per-scene ``state``, ``output_is_current``)
is computed fresh by ``services.render`` on every read from content fingerprints - never
copied from a job's own historical result - so the workspace cannot show a finished-looking
video that is out of date or missing sections.

``progress`` is parsed from the latest render job's payload (the worker writes it through
independent short transactions while the job runs); every field is optional because a job that
has only just been queued has not reported anything yet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from edutoon.api.schemas.jobs import JobResponse
from edutoon.api.schemas.scripts import ScriptResponse
from edutoon.api.schemas.storyboard import Template
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.render import RenderOutputRecord
from edutoon.services.render import RenderEnqueued, RenderEstimate, RenderSceneStatus, RenderStatus

Orientation = Literal["horizontal", "vertical"]
Quality = Literal["low", "high"]
SceneState = Literal["ready", "needs_render", "stale", "failed"]


class RenderEnqueueRequest(BaseModel):
    """Which video to make. Both fields are required: the caller confirms the orientation and
    quality they are asking for, rather than inheriting a default they may not have seen."""

    model_config = ConfigDict(extra="forbid")

    orientation: Orientation
    quality: Quality


class RenderRetryRequest(BaseModel):
    """Carries no fields: a retry repeats the last failed render as it was asked for. Exists so
    an unknown field in the body is rejected with 422 (rule 10)."""

    model_config = ConfigDict(extra="forbid")


class RenderEstimateResponse(BaseModel):
    chunk_count: int
    unique_chunk_count: int
    chunks_reusable: int
    chunks_to_render: int
    total_ms: int
    width: int
    height: int
    fps: int
    estimated_output_bytes: int
    required_free_bytes: int

    @classmethod
    def from_estimate(cls, estimate: RenderEstimate) -> RenderEstimateResponse:
        return cls(**{name: getattr(estimate, name) for name in RenderEstimate.__slots__})


class RenderEnqueueResponse(BaseModel):
    job: JobResponse
    estimate: RenderEstimateResponse

    @classmethod
    def from_enqueued(cls, enqueued: RenderEnqueued) -> RenderEnqueueResponse:
        return cls(
            job=JobResponse.from_record(enqueued.job),
            estimate=RenderEstimateResponse.from_estimate(enqueued.estimate),
        )


class RenderProgressResponse(BaseModel):
    """Live progress of a render job. ``percent`` is real work done (chunks rendered, plus one
    unit for assembly); ``eta_seconds`` is only present once enough chunks have finished to
    estimate from."""

    stage: str | None = None
    chunks_total: int | None = None
    chunks_reused: int | None = None
    chunks_to_render: int | None = None
    chunks_done: int | None = None
    chunks_complete: int | None = None
    segment_index: int | None = None
    segment_count: int | None = None
    scene_index: int | None = None
    scene_count: int | None = None
    percent: int | None = None
    eta_seconds: int | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_job(cls, job: JobRecord | None) -> RenderProgressResponse | None:
        raw: Any = job.payload.get("progress") if job is not None else None
        if not isinstance(raw, dict):
            return None
        fields: dict[str, Any] = {}
        for name in cls.model_fields:
            value = raw.get(name)
            if name == "updated_at":
                try:
                    fields[name] = datetime.fromisoformat(value) if isinstance(value, str) else None
                except ValueError:
                    fields[name] = None
            elif name == "stage":
                fields[name] = value if isinstance(value, str) else None
            else:
                is_int = isinstance(value, int) and not isinstance(value, bool)
                fields[name] = value if is_int else None
        return cls(**fields)


class RenderProgressStatusResponse(BaseModel):
    """The cheap poll: the latest render job and its progress, without re-evaluating the
    storyboard."""

    job: JobResponse | None
    progress: RenderProgressResponse | None


class RenderSceneResponse(BaseModel):
    position: int
    segment_id: UUID
    segment_index: int
    scene_index: int
    scene_count: int
    template: Template
    start_ms: int
    duration_ms: int
    state: SceneState

    @classmethod
    def from_status(cls, scene: RenderSceneStatus) -> RenderSceneResponse:
        return cls(
            position=scene.position,
            segment_id=scene.segment_id,
            segment_index=scene.segment_index,
            scene_index=scene.scene_index,
            scene_count=scene.scene_count,
            template=scene.template,  # type: ignore[arg-type]
            start_ms=scene.start_ms,
            duration_ms=scene.duration_ms,
            state=scene.state,
        )


class RenderOutputResponse(BaseModel):
    orientation: Orientation
    quality: Quality
    width: int
    height: int
    fps: int
    duration_ms: int
    byte_size: int
    encoder: str
    renderer_version: str
    created_at: datetime
    updated_at: datetime
    is_current: bool

    @classmethod
    def from_record(cls, record: RenderOutputRecord, *, is_current: bool) -> RenderOutputResponse:
        return cls(
            orientation=record.orientation,  # type: ignore[arg-type]
            quality=record.quality,  # type: ignore[arg-type]
            width=record.width,
            height=record.height,
            fps=record.fps,
            duration_ms=record.duration_ms,
            byte_size=record.byte_size,
            encoder=record.encoder,
            renderer_version=record.renderer_version,
            created_at=record.created_at,
            updated_at=record.updated_at,
            is_current=is_current,
        )


class RenderStatusResponse(BaseModel):
    script: ScriptResponse
    orientation: Orientation
    quality: Quality
    job: JobResponse | None
    progress: RenderProgressResponse | None
    blockers: list[str]
    scenes: list[RenderSceneResponse]
    ready_count: int
    needs_render_count: int
    stale_count: int
    failed_count: int
    output: RenderOutputResponse | None
    output_is_current: bool
    estimate: RenderEstimateResponse | None
    total_ms: int | None
    is_complete: bool

    @classmethod
    def from_status(cls, status: RenderStatus) -> RenderStatusResponse:
        return cls(
            script=ScriptResponse.from_record(status.script),
            orientation=status.orientation,  # type: ignore[arg-type]
            quality=status.quality,  # type: ignore[arg-type]
            job=JobResponse.from_record(status.job) if status.job else None,
            progress=RenderProgressResponse.from_job(status.job),
            blockers=list(status.blockers),
            scenes=[RenderSceneResponse.from_status(scene) for scene in status.scenes],
            ready_count=status.ready_count,
            needs_render_count=status.needs_render_count,
            stale_count=status.stale_count,
            failed_count=status.failed_count,
            output=(
                RenderOutputResponse.from_record(status.output, is_current=status.output_is_current)
                if status.output
                else None
            ),
            output_is_current=status.output_is_current,
            estimate=(
                RenderEstimateResponse.from_estimate(status.estimate) if status.estimate else None
            ),
            total_ms=status.total_ms,
            is_complete=status.is_complete,
        )
