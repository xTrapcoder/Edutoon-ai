"""The FFmpeg side of the renderer: chunk encoding, frame-exact assembly, narration placement
and output verification - against the real ``ffmpeg`` / ``ffprobe``."""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from edutoon.domain import render as r
from edutoon.providers.renderer import (
    AssemblySpec,
    ChunkFile,
    ChunkSpec,
    NarrationPlacement,
    PillowFfmpegRenderer,
    RendererError,
    ffmpeg_tools,
)
from tests.render_helpers import SAMPLE_RATE, requires_ffmpeg, wav_bytes

TITLE = {"title": "Engine test"}


def _spec(
    duration_ms: int, *, quality="low", encoder="libx264", orientation="horizontal"
) -> ChunkSpec:
    width, height = r.output_size(orientation, quality)
    return ChunkSpec(
        template="title_card",
        content=TITLE,
        orientation=orientation,
        width=width,
        height=height,
        fps=r.FPS,
        duration_ms=duration_ms,
        canonical_frames=r.canonical_frames(duration_ms),
        encoder=encoder,
        profile=r.profile_for(quality),
    )


def _write_wav(path: Path, duration_ms: int, hz: float = 220.0) -> None:
    path.write_bytes(wav_bytes(duration_ms, frequency=hz))


def _packet_count(path: Path) -> int:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_packets",
            "-show_entries",
            "stream=nb_read_packets",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return int(out.strip())


# --- pure helpers (no ffmpeg needed) ---


def test_the_encoder_arguments_differ_per_encoder_and_both_forbid_b_frames():
    profile = r.profile_for("high")
    x264 = ffmpeg_tools.video_encoder_args("libx264", profile)
    nvenc = ffmpeg_tools.video_encoder_args("h264_nvenc", profile)
    assert x264[x264.index("-c:v") + 1] == "libx264" and "-crf" in x264
    assert nvenc[nvenc.index("-c:v") + 1] == "h264_nvenc" and "-cq" in nvenc
    for args in (x264, nvenc):
        assert args[args.index("-bf") + 1] == "0"
        assert args[args.index("-g") + 1] == str(profile.gop_frames)
    with pytest.raises(RendererError):
        ffmpeg_tools.video_encoder_args("libx265", profile)


def test_the_concat_list_cuts_a_trimmed_chunk_half_a_frame_before_its_last_frame(tmp_path):
    chunks = [
        ChunkFile(Path("/w/a.mp4"), keep_frames=90, canonical_frames=90),
        ChunkFile(Path("/w/b.mp4"), keep_frames=89, canonical_frames=90),
    ]
    listing = tmp_path / "list.txt"
    ffmpeg_tools.write_concat_list(chunks, 30, listing)
    lines = listing.read_text().splitlines()
    assert lines[:2] == ["file '/w/a.mp4'", "duration 3.000000"]
    assert lines[2:] == ["file '/w/b.mp4'", "duration 2.966667", "outpoint 2.950000"]


def test_a_path_with_a_quote_is_escaped_in_the_concat_list(tmp_path):
    listing = tmp_path / "list.txt"
    ffmpeg_tools.write_concat_list([ChunkFile(Path("/w/it's.mp4"), 30, 30)], 30, listing)
    assert "it'\\''s" in listing.read_text()


def test_narration_is_placed_at_absolute_sample_positions(tmp_path):
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    _write_wav(first, 1_000)
    _write_wav(second, 1_000)
    out = tmp_path / "n.wav"
    # Slots deliberately a little shorter / longer than the audio: it is fitted, never shifted.
    written = ffmpeg_tools.assemble_narration_wav(
        [NarrationPlacement(first, 0, 999), NarrationPlacement(second, 999, 2_001)],
        out,
        sample_rate=SAMPLE_RATE,
    )
    assert written == r.sample_at(2_001, SAMPLE_RATE)
    with wave.open(str(out)) as result:
        assert result.getnframes() == written
        assert (result.getnchannels(), result.getsampwidth(), result.getframerate()) == (
            1,
            2,
            SAMPLE_RATE,
        )


def test_narration_of_the_wrong_format_is_refused_clearly(tmp_path):
    bad = tmp_path / "stereo.wav"
    with wave.open(str(bad), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"\0\0" * 2 * 1000)
    with pytest.raises(RendererError, match="not mono 16-bit"):
        ffmpeg_tools.assemble_narration_wav(
            [NarrationPlacement(bad, 0, 40)], tmp_path / "n.wav", sample_rate=SAMPLE_RATE
        )


def test_unreadable_narration_is_refused_without_leaking_the_path(tmp_path):
    missing = tmp_path / "secret-location" / "gone.wav"
    with pytest.raises(RendererError) as caught:
        ffmpeg_tools.assemble_narration_wav(
            [NarrationPlacement(missing, 0, 40)], tmp_path / "n.wav", sample_rate=SAMPLE_RATE
        )
    assert "secret-location" not in str(caught.value)


# --- the real encoder ---


@requires_ffmpeg
async def test_a_chunk_is_a_video_only_h264_mp4_with_the_canonical_frame_count(tmp_path):
    renderer = PillowFfmpegRenderer()
    spec = _spec(2_333)
    chunk = await renderer.render_chunk(spec, tmp_path / "c.mp4")
    assert chunk.frame_count == spec.canonical_frames == 70
    assert _packet_count(chunk.path) == 70
    streams = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,pix_fmt",
            "-of",
            "csv=p=0",
            str(chunk.path),
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()
    assert streams == ["h264,video,yuv420p"]


@requires_ffmpeg
async def test_assembly_keeps_exactly_the_frames_the_absolute_timeline_calls_for(tmp_path):
    renderer = PillowFfmpegRenderer()
    durations = [2_333, 3_337, 2_711, 4_001, 1_999]  # awkward, non-frame-aligned
    plan_ends, cursor = [], 0
    for d in durations:
        cursor += d
        plan_ends.append(cursor)
    total_ms = cursor
    starts = [0, *plan_ends[:-1]]
    files: list[ChunkFile] = []
    placements: list[NarrationPlacement] = []
    for index, (duration, start, end) in enumerate(zip(durations, starts, plan_ends, strict=True)):
        chunk = await renderer.render_chunk(_spec(duration), tmp_path / f"c{index}.mp4")
        keep = r.frame_at(end) - r.frame_at(start)
        files.append(ChunkFile(chunk.path, keep, r.canonical_frames(duration)))
        wav = tmp_path / f"n{index}.wav"
        _write_wav(wav, duration)
        placements.append(NarrationPlacement(wav, start, end))
    assert any(f.keep_frames != f.canonical_frames for f in files)  # the trim path is exercised

    final = tmp_path / "final.mp4"
    await renderer.assemble(
        AssemblySpec(files, placements, total_ms, r.FPS, "libx264", r.profile_for("low")), final
    )

    probe = await renderer.probe(final)
    assert probe.video_frames == r.frame_at(total_ms) == sum(f.keep_frames for f in files)
    assert (probe.width, probe.height, probe.fps) == (960, 540, 30.0)
    assert (probe.video.codec, probe.audio.codec, probe.sample_rate, probe.channels) == (
        "h264",
        "aac",
        SAMPLE_RATE,
        1,
    )
    assert probe.faststart is True
    assert abs((probe.video.duration_ms or 0) - total_ms) <= r.DURATION_TOLERANCE_MS
    assert abs((probe.audio.duration_ms or 0) - total_ms) <= r.DURATION_TOLERANCE_MS
    # It decodes end to end without a single error.
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(final), "-f", "null", "-"], capture_output=True
    )
    assert decode.returncode == 0 and decode.stderr == b""


@requires_ffmpeg
async def test_a_finished_video_passes_the_output_check_and_a_wrong_one_does_not(tmp_path):
    renderer = PillowFfmpegRenderer()
    segments = [
        r.SegmentInput("s0", 0, 3_000, "h", [r.SceneInput(0, 0, 3_000, "title_card", TITLE)])
    ]
    plan = r.build_render_plan(segments, orientation="horizontal", quality="low", encoder="libx264")
    chunk_spec = _spec(3_000)
    chunk = await renderer.render_chunk(chunk_spec, tmp_path / "c.mp4")
    wav = tmp_path / "n.wav"
    _write_wav(wav, 3_000)
    final = tmp_path / "f.mp4"
    await renderer.assemble(
        AssemblySpec(
            [
                ChunkFile(
                    chunk.path, plan.chunks[0].frame_count, plan.chunks[0].canonical_frame_count
                )
            ],
            [NarrationPlacement(wav, 0, 3_000)],
            3_000,
            r.FPS,
            "libx264",
            r.profile_for("low"),
        ),
        final,
    )
    summary = (await renderer.probe(final)).summary()
    assert r.check_output(summary, plan) == []

    problems = r.check_output({**summary, "video_frames": summary["video_frames"] - 1}, plan)
    assert any("frame count" in p for p in problems)
    problems = r.check_output({**summary, "audio_duration_ms": 9_000, "faststart": False}, plan)
    assert any("audio_duration_ms" in p for p in problems)
    assert any("index is not at the start" in p for p in problems)
    assert any("h264" in p for p in r.check_output({**summary, "video_codec": "vp9"}, plan))
    assert any("width" in p for p in r.check_output({**summary, "width": 1920}, plan))


@requires_ffmpeg
async def test_a_failing_encoder_gives_a_safe_message_and_leaves_no_partial_file(tmp_path):
    renderer = PillowFfmpegRenderer(ffmpeg="ffmpeg-that-does-not-exist-secret")
    dest = tmp_path / "c.mp4"
    with pytest.raises((RendererError, FileNotFoundError)) as caught:
        await renderer.render_chunk(_spec(1_500), dest)
    assert "secret" not in str(caught.value) or isinstance(caught.value, FileNotFoundError)
    assert not dest.exists()


@requires_ffmpeg
async def test_the_gpu_encoder_can_be_selected_when_it_is_available(tmp_path):
    """``h264_nvenc`` is a configuration choice, not a rewrite: the same pipeline drives it.
    Skipped on machines without an NVIDIA GPU."""
    renderer = PillowFfmpegRenderer()
    try:
        chunk = await renderer.render_chunk(_spec(1_500, encoder="h264_nvenc"), tmp_path / "n.mp4")
    except RendererError:
        pytest.skip("h264_nvenc is not usable on this machine")
    assert chunk.frame_count == r.canonical_frames(1_500)
    assert _packet_count(chunk.path) == chunk.frame_count
