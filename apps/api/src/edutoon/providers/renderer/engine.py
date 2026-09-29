"""``PillowFfmpegRenderer`` - the Phase 10 rendering engine: a Pillow compositor feeding FFmpeg.

Implements :class:`~edutoon.providers.renderer.base.Renderer`. All the blocking work (drawing,
piping frames, running FFmpeg) happens in worker threads so it never stalls the event loop.
Frames of a still scene are the very same bytes, so a scene is drawn once per distinct
animation state and written as often as needed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import structlog

from edutoon.providers.renderer import ffmpeg_tools
from edutoon.providers.renderer.base import (
    AssemblySpec,
    ChunkSpec,
    MediaProbe,
    RenderedChunk,
    RendererError,
)
from edutoon.providers.renderer.compositor import SceneCompositor

log = structlog.get_logger()


class PillowFfmpegRenderer:
    def __init__(self, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    # -- chunks ---------------------------------------------------------------------

    async def render_chunk(self, spec: ChunkSpec, dest: Path) -> RenderedChunk:
        return await asyncio.to_thread(self._render_chunk_sync, spec, dest)

    def _render_chunk_sync(self, spec: ChunkSpec, dest: Path) -> RenderedChunk:
        started = time.perf_counter()
        try:
            compositor = SceneCompositor(
                template=spec.template,
                content=spec.content,
                orientation=spec.orientation,
                width=spec.width,
                height=spec.height,
                duration_ms=spec.duration_ms,
                enter_fade_ms=spec.enter_fade_ms,
                exit_fade_ms=spec.exit_fade_ms,
            )
        except Exception as exc:  # noqa: BLE001 - any drawing failure is a render failure
            log.error("render.scene_setup_failed", template=spec.template, error=repr(exc))
            raise RendererError("A scene could not be drawn.") from exc

        def frames() -> Iterator[bytes]:
            previous_key: object = None
            previous_bytes = b""
            for index in range(spec.canonical_frames):
                t_ms = index * 1000 / spec.fps
                key = compositor.state_key(t_ms)
                if key != previous_key:
                    previous_bytes = compositor.frame(t_ms).tobytes()
                    previous_key = key
                yield previous_bytes

        command = ffmpeg_tools.chunk_command(
            self._ffmpeg,
            width=spec.width,
            height=spec.height,
            fps=spec.fps,
            encoder=spec.encoder,
            profile=spec.profile,
            dest=dest,
        )
        try:
            ffmpeg_tools.encode_frames(command, frames())
        except RendererError:
            dest.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            dest.unlink(missing_ok=True)
            log.error("render.scene_draw_failed", template=spec.template, error=repr(exc))
            raise RendererError("A scene could not be drawn.") from exc
        log.info(
            "render.chunk_encoded",
            template=spec.template,
            frames=spec.canonical_frames,
            seconds=round(time.perf_counter() - started, 3),
        )
        return RenderedChunk(
            path=dest, frame_count=spec.canonical_frames, byte_size=dest.stat().st_size
        )

    # -- assembly -------------------------------------------------------------------

    async def assemble(self, spec: AssemblySpec, dest: Path) -> None:
        await asyncio.to_thread(self._assemble_sync, spec, dest)

    def _assemble_sync(self, spec: AssemblySpec, dest: Path) -> None:
        work = dest.parent
        narration = work / f"{dest.stem}.narration.wav"
        concat_list = work / f"{dest.stem}.concat.txt"
        try:
            ffmpeg_tools.assemble_narration_wav(
                spec.narration, narration, sample_rate=spec.profile.audio_sample_rate
            )
            ffmpeg_tools.write_concat_list(spec.chunks, spec.fps, concat_list)
            command = ffmpeg_tools.mux_command(
                self._ffmpeg,
                concat_list=concat_list,
                narration=narration,
                profile=spec.profile,
                dest=dest,
            )
            ffmpeg_tools.run(command, what="final assembly")
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        finally:
            narration.unlink(missing_ok=True)
            concat_list.unlink(missing_ok=True)

    async def probe(self, path: Path) -> MediaProbe:
        return await asyncio.to_thread(ffmpeg_tools.probe_media, self._ffprobe, path)
