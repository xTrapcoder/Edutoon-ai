"""What the Phase 10 render tests share: a project that is exactly what a render needs (an
approved script, ready narration audio in object storage, a complete storyboard), and a renderer
that counts and can be told to fail.

Everything is real except the narration: audio is a generated low-level tone written as a proper
mono 22.05 kHz WAV (the format Piper produces) and uploaded to the local MinIO, so the tests do
not depend on the Piper voice - the end-to-end verification script uses Piper itself. The
database is the real local Postgres (rolled back per test), storage the real local MinIO, and
the renderer the real Pillow + FFmpeg one.
"""

from __future__ import annotations

import asyncio
import io
import math
import shutil
import struct
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from edutoon.core.config import get_settings
from edutoon.domain.script_audio import compute_content_hash, segment_audio_storage_key
from edutoon.domain.storyboard_timing import (
    allocate_scene_durations,
    scene_timings,
    segment_absolute_starts,
)
from edutoon.providers import tts as tts_provider
from edutoon.providers.renderer import (
    AssemblySpec,
    ChunkSpec,
    MediaProbe,
    PillowFfmpegRenderer,
    RenderedChunk,
    RendererError,
)
from edutoon.providers.storage import Storage
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import render as render_repo
from edutoon.repositories import script_segment_audio as audio_repo
from edutoon.repositories import script_segment_scenes as scenes_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.projects import ProjectRecord
from edutoon.repositories.script_segment_audio import ScriptSegmentAudioRecord
from edutoon.repositories.script_segment_scenes import NewScene
from edutoon.repositories.scripts import NewScriptSegment, ScriptRecord, ScriptSegmentRecord
from edutoon.services import script_audio as script_audio_service

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg / ffprobe are not installed",
)

MODEL = "claude-sonnet-5"
SAMPLE_RATE = 22_050

_LABEL_KEYS = ("title", "text", "caption", "content")


def wav_bytes(duration_ms: int, *, frequency: float = 220.0) -> bytes:
    """A mono 16-bit 22.05 kHz WAV of ``duration_ms`` (a quiet tone, so it is real audio)."""
    frames = round(duration_ms * SAMPLE_RATE / 1000)
    samples = b"".join(
        struct.pack("<h", round(1500 * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE)))
        for i in range(frames)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples)
    return buffer.getvalue()


def tone_hz(segment_index: int) -> float:
    """Each segment's narration is a different pure tone, so where one ends and the next
    begins can be measured in the finished video's audio."""
    return 220.0 * (segment_index + 1)


def scene_content(template: str, label: str) -> dict[str, Any]:
    illustration = {"description": f"A figure for {label}", "subject_labels": ["one", "two"]}
    return {
        "title_card": {"title": label, "subtitle": "A short subtitle"},
        "bullet_points": {"title": label, "bullets": ["First point", "Second point"]},
        "labelled_diagram": {
            "illustration": illustration,
            "labels": [{"text": label, "position": "top"}, {"text": "Part", "position": "left"}],
        },
        "split_text_illustration": {"text": label, "illustration": illustration},
        "full_bleed_illustration": {"illustration": illustration, "caption": label},
        "formula_or_code": {"content": label, "kind": "formula", "label": "Formula"},
    }[template]


def label_of(content: Any) -> str:
    """The human label a test scene was created with (used to tell which scenes were rendered)."""
    for key in _LABEL_KEYS:
        value = content.get(key)
        if isinstance(value, str):
            return value
    for label in content.get("labels", []):
        return str(label["text"])
    return str(content.get("illustration", {}).get("description", ""))


@dataclass(slots=True)
class Built:
    project: ProjectRecord
    script: ScriptRecord
    segments: list[ScriptSegmentRecord]
    audio: dict[UUID, ScriptSegmentAudioRecord]
    labels: list[list[str]]  # scene labels per segment
    storage_keys: list[tuple[str, str]]


DEFAULT_LAYOUT: list[list[str]] = [
    ["title_card", "bullet_points"],
    ["labelled_diagram", "split_text_illustration"],
    ["formula_or_code", "full_bleed_illustration"],
]
DEFAULT_DURATIONS = [4_001, 5_003, 4_502]


async def build_project(
    db_session: Any,
    storage: Storage,
    *,
    layout: Sequence[Sequence[str]] = DEFAULT_LAYOUT,
    durations_ms: Sequence[int] = DEFAULT_DURATIONS,
    orientation: str = "horizontal",
    texts: Sequence[str] | None = None,
    project: ProjectRecord | None = None,
) -> Built:
    """A project with an approved script, ready audio in object storage and a complete
    storyboard. ``layout[i]`` is the list of scene templates of segment ``i``; a segment with an
    empty list has no scenes (the degenerate short-segment case)."""
    settings = get_settings()
    if project is None:
        owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
        project = await projects_repo.create(
            db_session,
            owner_id=owner.id,
            title="Photosynthesis",
            source_type="topic",
            orientation=orientation,
        )
    narrations = list(
        texts or [f"This is narration for section number {i}." for i in range(len(layout))]
    )
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    segments = await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(segment_index=i, narration_text=text, estimated_duration_ms=d)
            for i, (text, d) in enumerate(zip(narrations, durations_ms, strict=True))
        ],
    )
    approved = await scripts_repo.set_status(
        db_session, project_id=project.id, script_id=script.id, status="approved"
    )
    assert approved is not None

    voice = script_audio_service.voice_selection_from_settings(settings, project.language)
    engine_version = tts_provider.engine_version()
    audio: dict[UUID, ScriptSegmentAudioRecord] = {}
    keys: list[tuple[str, str]] = []
    measured: list[int] = []
    for index, (segment, text, requested) in enumerate(
        zip(segments, narrations, durations_ms, strict=True)
    ):
        data = wav_bytes(requested, frequency=tone_hz(index))
        key = segment_audio_storage_key(
            project_id=project.id, script_id=script.id, segment_id=segment.id
        )
        await storage.put_object(
            bucket=settings.BUCKET_SEGMENTS, key=key, body=data, content_type="audio/wav"
        )
        keys.append((settings.BUCKET_SEGMENTS, key))
        duration = round(round(requested * SAMPLE_RATE / 1000) / SAMPLE_RATE * 1000)
        measured.append(duration)
        audio[segment.id] = await audio_repo.upsert(
            db_session,
            script_segment_id=segment.id,
            status="ready",
            storage_bucket=settings.BUCKET_SEGMENTS,
            storage_key=key,
            format="wav",
            sample_rate=SAMPLE_RATE,
            channels=1,
            measured_duration_ms=duration,
            voice_model=voice.voice_model,
            is_temporary_voice_substitute=voice.is_temporary_substitute,
            content_hash=compute_content_hash(
                narration_text=text,
                voice_model=voice.voice_model,
                engine_version=engine_version,
                sample_rate=SAMPLE_RATE,
            ),
        )
    final = await scripts_repo.set_measured_duration(
        db_session, project_id=project.id, script_id=script.id, measured_duration_ms=sum(measured)
    )
    assert final is not None

    starts = segment_absolute_starts(measured)
    labels: list[list[str]] = []
    for index, (segment, templates) in enumerate(zip(segments, layout, strict=True)):
        segment_labels = [f"Seg{index}Scene{n}" for n in range(len(templates))]
        labels.append(segment_labels)
        if not templates:
            continue
        timings = scene_timings(
            starts[index], allocate_scene_durations(measured[index], len(templates))
        )
        await scenes_repo.replace_for_segment(
            db_session,
            script_segment_id=segment.id,
            scenes=[
                NewScene(
                    scene_index=timing.scene_index,
                    start_ms=timing.start_ms,
                    duration_ms=timing.duration_ms,
                    template=template,
                    content=scene_content(template, label),
                    source_audio_content_hash=audio[segment.id].content_hash,
                )
                for timing, template, label in zip(timings, templates, segment_labels, strict=True)
            ],
        )
    return Built(project, final, segments, audio, labels, keys)


async def remove_objects(db_session: Any, storage: Storage, built: Built) -> None:
    """Delete every object a test created: the audio, plus whatever renders left behind - found
    by listing the project's own render prefix (a test may have removed the rows that name them)."""
    settings = get_settings()
    for bucket, key in built.storage_keys:
        await storage.delete_object(bucket=bucket, key=key)
    for location in await render_repo.list_storage_locations_for_project(
        db_session, built.project.id
    ):
        await storage.delete_object(bucket=location.bucket, key=location.key)
    prefix = f"projects/{built.project.id}/renders/"
    client = storage._client  # noqa: SLF001 - test cleanup lists a prefix, which the app never does
    for bucket in (settings.BUCKET_SEGMENTS, settings.BUCKET_OUTPUTS):
        listing = await asyncio.to_thread(client.list_objects_v2, Bucket=bucket, Prefix=prefix)
        for item in listing.get("Contents", []):
            await storage.delete_object(bucket=bucket, key=item["Key"])


class CountingRenderer:
    """The real renderer, recording every chunk it draws and every assembly - and able to fail
    or 'die' on demand, to prove what survives."""

    def __init__(self, inner: PillowFfmpegRenderer | None = None) -> None:
        self.inner = inner or PillowFfmpegRenderer()
        self.chunks: list[str] = []
        self.assemblies = 0
        self.fail_label: str | None = None
        self.die_after: int | None = None
        self.fail_assembly = False

    async def render_chunk(self, spec: ChunkSpec, dest: Path) -> RenderedChunk:
        label = label_of(spec.content)
        if self.die_after is not None and len(self.chunks) >= self.die_after:
            raise KeyboardInterrupt("worker killed")  # not an Exception: nothing can catch it
        if self.fail_label is not None and label == self.fail_label:
            raise RendererError("boom (test)")
        self.chunks.append(label)
        return await self.inner.render_chunk(spec, dest)

    async def assemble(self, spec: AssemblySpec, dest: Path) -> None:
        self.assemblies += 1
        if self.fail_assembly:
            raise RendererError("assembly boom (test)")
        await self.inner.assemble(spec, dest)

    async def probe(self, path: Path) -> MediaProbe:
        return await self.inner.probe(path)
