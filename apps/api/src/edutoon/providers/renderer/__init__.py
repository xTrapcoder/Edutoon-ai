"""The video rendering provider (Phase 10). See :mod:`edutoon.providers.renderer.base` for the
boundary and :mod:`edutoon.providers.renderer.engine` for the implementation."""

from edutoon.providers.renderer.base import (
    AssemblySpec,
    ChunkFile,
    ChunkSpec,
    MediaProbe,
    NarrationPlacement,
    RenderedChunk,
    Renderer,
    RendererError,
)
from edutoon.providers.renderer.engine import PillowFfmpegRenderer


def get_renderer(*, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> Renderer:
    return PillowFfmpegRenderer(ffmpeg=ffmpeg, ffprobe=ffprobe)


__all__ = [
    "AssemblySpec",
    "ChunkFile",
    "ChunkSpec",
    "MediaProbe",
    "NarrationPlacement",
    "PillowFfmpegRenderer",
    "RenderedChunk",
    "Renderer",
    "RendererError",
    "get_renderer",
]
