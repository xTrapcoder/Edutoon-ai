"""Request/response schemas for
``/v1/projects/{project_id}/scripts/{script_id}/synthesis`` and
``.../segments/{segment_id}/audio`` (Phase 8).

``AudioStatus`` mirrors the ``ck_script_segment_audio_status`` CHECK
constraint (migration 0014) exactly, the same pattern as ``ScriptStatus`` /
``JobKind``. Storage internals (bucket, key) are never exposed - only what a
caller needs to know whether audio exists, play it, and see which voice made
it; ``is_temporary_voice_substitute`` is always present so an en-IN
segment's audio is never mistaken for genuine Indian English (see
apps/api/README.md, "Text-to-speech").
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from edutoon.api.schemas.jobs import JobResponse
from edutoon.api.schemas.scripts import ScriptResponse
from edutoon.repositories.script_segment_audio import ScriptSegmentAudioRecord
from edutoon.services.script_audio import SegmentAudioDetail, SynthesisStatus

AudioStatus = Literal["ready", "failed"]


class SynthesisRequest(BaseModel):
    """Carries no fields yet: synthesis takes its voice/settings from
    configuration, not the caller. Exists so an unknown field in the request
    body is rejected with 422 (rule 10) rather than silently ignored, the
    same pattern as ``ScriptGenerateRequest``.
    """

    model_config = ConfigDict(extra="forbid")


class SegmentAudioResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: AudioStatus
    format: str | None
    sample_rate: int | None
    channels: int | None
    measured_duration_ms: int | None
    voice_model: str
    is_temporary_voice_substitute: bool
    error: str | None
    updated_at: datetime

    @classmethod
    def from_record(cls, record: ScriptSegmentAudioRecord) -> SegmentAudioResponse:
        return cls.model_validate(record)


class SynthesisSegmentResponse(BaseModel):
    id: UUID
    segment_index: int
    estimated_duration_ms: int
    audio: SegmentAudioResponse | None

    @classmethod
    def from_detail(cls, detail: SegmentAudioDetail) -> SynthesisSegmentResponse:
        return cls(
            id=detail.segment.id,
            segment_index=detail.segment.segment_index,
            estimated_duration_ms=detail.segment.estimated_duration_ms,
            audio=SegmentAudioResponse.from_record(detail.audio) if detail.audio else None,
        )


class SynthesisStatusResponse(BaseModel):
    script: ScriptResponse
    job: JobResponse | None
    segments: list[SynthesisSegmentResponse]

    @classmethod
    def from_status(cls, synthesis_status: SynthesisStatus) -> SynthesisStatusResponse:
        return cls(
            script=ScriptResponse.from_record(synthesis_status.script),
            job=JobResponse.from_record(synthesis_status.job) if synthesis_status.job else None,
            segments=[
                SynthesisSegmentResponse.from_detail(detail)
                for detail in synthesis_status.segments
            ],
        )
