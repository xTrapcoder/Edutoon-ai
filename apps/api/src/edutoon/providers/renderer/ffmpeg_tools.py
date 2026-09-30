"""FFmpeg / ffprobe plumbing for the Pillow + FFmpeg renderer.

This is the only module that builds an FFmpeg command line or reads ffprobe's output. Every
function is synchronous (the engine runs them in worker threads) and raises
:class:`~edutoon.providers.renderer.base.RendererError` with a message written for the
project's owner; the command line and FFmpeg's stderr go to the log only.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
import wave
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import structlog

from edutoon.domain.render import EncodingProfile, sample_at
from edutoon.providers.renderer.base import (
    ChunkFile,
    MediaProbe,
    NarrationPlacement,
    RendererError,
    StreamProbe,
)

log = structlog.get_logger()

_STDERR_TAIL_CHARS = 2000
_BLOCK_SAMPLES = 1 << 16


def _fail(what: str, *, command: Sequence[str], stderr: str) -> RendererError:
    """A :class:`RendererError` whose message is safe to show; the detail is logged."""
    log.error(
        "render.ffmpeg_failed",
        step=what,
        command=" ".join(str(part) for part in command),
        stderr=stderr[-_STDERR_TAIL_CHARS:],
    )
    return RendererError(f"The video encoder could not finish the {what} step.")


def video_encoder_args(encoder: str, profile: EncodingProfile) -> list[str]:
    """The encoder-specific arguments. ``libx264`` is constant-quality (CRF); ``h264_nvenc``
    is the closest hardware equivalent (constant-quality VBR). Both are forced to no B-frames
    and a fixed GOP so a chunk's last frame can be dropped without re-encoding and every chunk
    starts on a keyframe."""
    common = [
        "-pix_fmt",
        profile.pix_fmt,
        "-g",
        str(profile.gop_frames),
        "-bf",
        str(profile.bframes),
    ]
    if encoder == "libx264":
        return [
            "-c:v",
            "libx264",
            "-preset",
            profile.preset,
            "-crf",
            str(profile.crf),
            "-profile:v",
            profile.x264_profile,
            "-level:v",
            profile.x264_level,
            "-x264-params",
            profile.x264_params,
            *common,
        ]
    if encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            str(profile.crf + 1),
            "-b:v",
            "0",
            "-profile:v",
            profile.x264_profile,
            "-level:v",
            profile.x264_level,
            *common,
        ]
    raise RendererError(f"Unsupported video encoder {encoder!r}.")


# BT.709, limited range: the colour space browsers assume for HD video. The conversion is
# stated explicitly so the picture does not shift colour on playback.
_COLOUR_FILTER = (
    "scale=in_range=full:out_range=tv:out_color_matrix=bt709:flags=bicubic,format={pix_fmt}"
)
_COLOUR_TAGS = [
    "-colorspace",
    "bt709",
    "-color_primaries",
    "bt709",
    "-color_trc",
    "bt709",
    "-color_range",
    "tv",
]


def chunk_command(
    ffmpeg: str,
    *,
    width: int,
    height: int,
    fps: int,
    encoder: str,
    profile: EncodingProfile,
    dest: Path,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
        "-vf",
        _COLOUR_FILTER.format(pix_fmt=profile.pix_fmt),
        *video_encoder_args(encoder, profile),
        *_COLOUR_TAGS,
        "-r",
        str(fps),
        "-video_track_timescale",
        str(fps * 1000),
        "-f",
        "mp4",
        str(dest),
    ]


def encode_frames(command: list[str], frames: Iterable[bytes]) -> None:
    """Run the chunk encoder, feeding it raw frames on stdin. A failure of any kind (the
    encoder exits early, a broken pipe) becomes a :class:`RendererError`."""
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(  # noqa: S603 - argv list, no shell
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr_file
        )
        assert process.stdin is not None
        try:
            for frame in frames:
                process.stdin.write(frame)
        except (BrokenPipeError, OSError):
            pass  # the encoder died; its exit status and stderr say why, below
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()
        code = process.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read().decode("utf-8", errors="replace")
    if code != 0:
        raise _fail("scene encoding", command=command, stderr=stderr)


def run(command: list[str], *, what: str) -> None:
    result = subprocess.run(  # noqa: S603 - argv list, no shell
        command, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise _fail(what, command=command, stderr=result.stderr.decode("utf-8", errors="replace"))


# --- narration --------------------------------------------------------------------


def assemble_narration_wav(
    placements: Sequence[NarrationPlacement], dest: Path, *, sample_rate: int
) -> int:
    """Write the whole narration as one mono 16-bit WAV, streaming: each segment is fitted to
    exactly the samples between its own absolute start and the next segment's (padded with a
    fraction of a millisecond of silence, or trimmed by less than that), so a segment's audio
    starts at ``sample_at(start_ms)`` whatever the segments before it were - positions never
    accumulate error. Returns the number of samples written."""
    written = 0
    with wave.open(str(dest), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        for placement in placements:
            target = sample_at(placement.end_ms, sample_rate) - sample_at(
                placement.start_ms, sample_rate
            )
            remaining = target
            try:
                with wave.open(str(placement.path), "rb") as source:
                    if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (
                        1,
                        2,
                        sample_rate,
                    ):
                        raise RendererError(
                            "A section's narration is not mono 16-bit "
                            f"{sample_rate} Hz audio; narrate the script again."
                        )
                    while remaining > 0:
                        block = source.readframes(min(remaining, _BLOCK_SAMPLES))
                        if not block:
                            break
                        out.writeframes(block)
                        remaining -= len(block) // 2
            except (wave.Error, OSError, EOFError) as exc:
                log.error("render.narration_unreadable", path=str(placement.path), error=str(exc))
                raise RendererError("A section's narration audio could not be read.") from exc
            while remaining > 0:
                pad = min(remaining, _BLOCK_SAMPLES)
                out.writeframes(b"\x00\x00" * pad)
                remaining -= pad
            written += target
    return written


# --- assembly -----------------------------------------------------------------------


def write_concat_list(chunks: Sequence[ChunkFile], fps: int, dest: Path) -> None:
    """The concat-demuxer list. Each chunk states its exact duration (``keep_frames / fps``) so
    the next one starts on the frame the absolute rule assigns it - the demuxer never derives a
    start from a rounded stream duration - and a chunk that must lose its last frame is cut
    half a frame before it (``outpoint``), which needs no re-encoding because chunks have no
    B-frames."""
    lines: list[str] = []
    for chunk in chunks:
        escaped = str(chunk.path).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
        lines.append(f"duration {chunk.keep_frames / fps:.6f}")
        if chunk.keep_frames != chunk.canonical_frames:
            outpoint_us = (2 * chunk.keep_frames - 1) * 1_000_000 // (2 * fps)
            lines.append(f"outpoint {outpoint_us / 1_000_000:.6f}")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mux_command(
    ffmpeg: str, *, concat_list: Path, narration: Path, profile: EncodingProfile, dest: Path
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-i",
        str(narration),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        f"{profile.audio_bitrate_kbps}k",
        "-ar",
        str(profile.audio_sample_rate),
        "-ac",
        str(profile.audio_channels),
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(dest),
    ]


# --- probe ------------------------------------------------------------------------------


def _ms(seconds: Any) -> int | None:
    try:
        return round(float(seconds) * 1000)
    except (TypeError, ValueError):
        return None


def _fps(rate: str) -> float:
    try:
        numerator, denominator = rate.split("/")
        return int(numerator) / int(denominator) if int(denominator) else 0.0
    except (ValueError, AttributeError):
        return 0.0


def _is_constant_frame_rate(video: dict[str, Any]) -> bool:
    """ffprobe's ``r_frame_rate`` is the smallest rate every frame timestamp is a multiple of;
    ``avg_frame_rate`` is frames over duration. They agree exactly for constant-frame-rate video
    and differ as soon as a timestamp is out of step."""
    real = str(video.get("r_frame_rate", ""))
    average = str(video.get("avg_frame_rate", ""))
    return _fps(real) > 0 and _fps(real) == _fps(average)


def probe_media(ffprobe: str, path: Path) -> MediaProbe:
    command = [
        ffprobe,
        "-v",
        "error",
        "-count_packets",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise _fail(
            "verification", command=command, stderr=result.stderr.decode("utf-8", "replace")
        )
    try:
        data = json.loads(result.stdout)
        streams = {stream["codec_type"]: stream for stream in data["streams"]}
        video, audio = streams["video"], streams["audio"]
        format_ = data["format"]
        container_ms = _ms(format_["duration"])
        if container_ms is None:
            raise KeyError("duration")
        return MediaProbe(
            container_duration_ms=container_ms,
            size_bytes=int(format_["size"]),
            video=StreamProbe(video["codec_name"], _ms(video.get("duration")), {}),
            audio=StreamProbe(audio["codec_name"], _ms(audio.get("duration")), {}),
            width=int(video["width"]),
            height=int(video["height"]),
            fps=_fps(video.get("avg_frame_rate", "0/1")),
            video_frames=int(video.get("nb_read_packets", 0)),
            pix_fmt=str(video.get("pix_fmt", "")),
            video_profile=str(video.get("profile", "")),
            sample_rate=int(audio["sample_rate"]),
            channels=int(audio["channels"]),
            faststart=moov_before_mdat(path),
            video_level=int(video.get("level", 0)),
            constant_frame_rate=_is_constant_frame_rate(video),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise _fail(
            "verification", command=command, stderr=result.stdout.decode("utf-8", "replace")
        ) from exc


def moov_before_mdat(path: Path) -> bool:
    """Whether the MP4's index (``moov``) precedes its media (``mdat``) - i.e. it starts
    playing and seeks before it has fully downloaded. Reads only top-level box headers."""
    seen: list[str] = []
    with path.open("rb") as handle:
        while len(seen) < 16:
            header = handle.read(8)
            if len(header) < 8:
                break
            size = int.from_bytes(header[:4], "big")
            kind = header[4:8].decode("latin-1")
            seen.append(kind)
            if kind in ("moov", "mdat"):
                break
            if size == 1:
                size = int.from_bytes(handle.read(8), "big")
                handle.seek(size - 16, 1)
            elif size == 0:
                break
            else:
                handle.seek(size - 8, 1)
    return "moov" in seen and ("mdat" not in seen or seen.index("moov") < seen.index("mdat"))
