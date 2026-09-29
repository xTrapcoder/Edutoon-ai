"""The rendering boundary (Phase 10).

``services.render`` talks to a :class:`Renderer` and nothing else; how a scene becomes pixels and
how chunks become an MP4 is the implementation's business
(:class:`edutoon.providers.renderer.engine.PillowFfmpegRenderer`: a Pillow compositor feeding
FFmpeg). A different engine - a headless browser, Remotion - would implement the same three
methods and change nothing above this line: not the projects, scripts, storyboard, job or
API code.

Everything crossing the boundary is plain data (paths, integers, strings): no Pillow image, no
FFmpeg argument list, no subprocess handle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from edutoon.domain.render import EncodingProfile


class RendererError(Exception):
    """A render step failed. ``str(error)`` is written for the project's owner and is safe to
    show verbatim; the engine's own diagnostics (command lines, FFmpeg's stderr, paths) go to
    the server log and are never part of this message."""


@dataclass(frozen=True, slots=True)
class ChunkSpec:
    """Everything needed to draw one scene: its template and content, its duration, and the
    frame count it is rendered with (``canonical_frames`` - see ``domain.render``)."""

    template: str
    content: Mapping[str, Any]
    orientation: str
    width: int
    height: int
    fps: int
    duration_ms: int
    canonical_frames: int
    encoder: str
    profile: EncodingProfile


@dataclass(frozen=True, slots=True)
class RenderedChunk:
    path: Path
    frame_count: int
    byte_size: int


@dataclass(frozen=True, slots=True)
class ChunkFile:
    """One rendered chunk as the assembly needs it: the file, and how many of its frames
    the absolute timeline keeps (all of them, or all but the last)."""

    path: Path
    keep_frames: int
    canonical_frames: int


@dataclass(frozen=True, slots=True)
class NarrationPlacement:
    """One segment's narration WAV and where it sits on the timeline. ``end_ms`` is where the
    next segment starts (or the end of the video): the audio is fitted to
    ``[start_ms, end_ms)`` in whole samples, so audio positions are absolute too."""

    path: Path
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class AssemblySpec:
    chunks: Sequence[ChunkFile]
    narration: Sequence[NarrationPlacement]
    total_ms: int
    fps: int
    encoder: str
    profile: EncodingProfile


@dataclass(frozen=True, slots=True)
class StreamProbe:
    codec: str
    duration_ms: int | None
    detail: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """What ``ffprobe`` reports about a finished video, reduced to what we verify."""

    container_duration_ms: int
    size_bytes: int
    video: StreamProbe
    audio: StreamProbe
    width: int
    height: int
    fps: float
    video_frames: int
    pix_fmt: str
    video_profile: str
    sample_rate: int
    channels: int
    faststart: bool

    def summary(self) -> dict[str, Any]:
        return {
            "container_duration_ms": self.container_duration_ms,
            "video_duration_ms": self.video.duration_ms,
            "audio_duration_ms": self.audio.duration_ms,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_frames": self.video_frames,
            "video_codec": self.video.codec,
            "video_profile": self.video_profile,
            "pix_fmt": self.pix_fmt,
            "audio_codec": self.audio.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "faststart": self.faststart,
        }


class Renderer(Protocol):
    """A rendering engine."""

    async def render_chunk(self, spec: ChunkSpec, dest: Path) -> RenderedChunk:
        """Draw one scene and encode it, video only, to ``dest``."""
        ...

    async def assemble(self, spec: AssemblySpec, dest: Path) -> None:
        """Join chunks and narration into the finished MP4 at ``dest``. Streams from disk: it
        never holds the video in memory."""
        ...

    async def probe(self, path: Path) -> MediaProbe:
        """Describe a finished video."""
        ...
