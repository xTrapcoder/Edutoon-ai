"""Phase 10's rendering model - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services, and no configuration reads).

**What this module decides.** Given an approved script's *measured* narration and its
Phase 9 scenes, it decides everything about a render that is not pixels or bytes:

- the production frame rate and the encoding profiles (:data:`FPS`, :data:`PROFILES`);
- how a millisecond position becomes a frame (:func:`frame_at`);
- the **render plan** (:func:`build_render_plan`): one *chunk* per scene, in order, each
  with an absolute start/end, an exact frame count and a **fingerprint**
  (:attr:`PlannedChunk.chunk_hash`);
- whether an artifact is still valid (:func:`classify_scene`) and what a render of a
  given plan still has to do (:func:`chunks_to_render`).

**The hierarchy.** ``PROJECT > TOPIC > SEGMENT > SCENE > VISUAL > CHUNK > VIDEO``. A script
segment belongs to a topic (through the script's sections), a scene belongs to one segment,
and *one scene is one render chunk*. Nothing above the scene needs its own artifact: a
topic or a segment is just a set of scenes, so "invalidate a segment / a topic" is the same
operation as "invalidate its scenes' fingerprints" and touches nothing else.

**Dependency graph** (an arrow means "is an input to"; the last column is what changes a
chunk's fingerprint):

::

    project settings ──► orientation ─────────────┐
                    └──► quality ─────────────────┤
    renderer version / template look / encoder ───┤
    fps, encoding profile ────────────────────────┤
    narration audio ──► measured duration ──► scene duration ─┤──► CHUNK ──► FINAL VIDEO
    storyboard scene (template + content) ────────┘               ▲
    narration audio (per segment, at its start)  ────────────────┘ (audio track only)

* An illustrated scene's content may hold a procedural visual (Phase 11), so an edit to a visual
  is an edit to that scene's content and changes only that scene's fingerprint. Such a scene's
  fingerprint also carries the drawing versions of the code that draws it
  (``domain.visual.visual_drawing_id``: the shared framework, the visual's form, the template's
  fallback) - so bumping one form's version re-renders only that form's scenes, and text-only
  templates, which carry none, are never touched by a change to visual drawing.
* How a scene joins its neighbours is an input too (Phase 11): straight cuts inside a topic, a
  fade at a topic boundary and at the ends of the video (``PlannedChunk.enter_fade_ms`` /
  ``exit_fade_ms``, from the ``RENDER_FADE_*`` settings). A cut edge carries no fade length,
  so retuning the fades re-renders only the scenes that fade.
* A chunk is *video only*. It depends on the scene's template, content and duration - not
  on the narration audio itself, and not on where in the video it sits. Re-narrating one
  segment therefore changes the durations (and, via the storyboard, the content) of that
  segment's scenes only; every other segment's chunks keep their fingerprints and are
  reused, even though they now start at a different absolute time.
* Orientation and quality are inputs of every chunk (size, layout, encoder settings), so
  changing either invalidates video chunks - and nothing else: narration, the storyboard
  and the audio track are untouched.
* The narration is assembled into one audio track at final-assembly time, from the segments'
  own audio files; it is never an input to a chunk.
* The **output fingerprint** (:func:`output_fingerprint`) covers the ordered chunk
  fingerprints plus the narration content hashes, so the final video is rebuilt exactly
  when one of them - or the renderer, orientation, quality or encoder - changes.

**Never timestamps.** Every dependency test here is a comparison of content hashes.

**Timing.** Milliseconds are canonical; frame numbers are derived, never stored as truth.
A frame position is always computed from an *absolute* millisecond position,
``frame = round(t_ms * fps / 1000)`` (:func:`frame_at`, half-up, integer arithmetic), so
rounding never accumulates: chunk *k* covers frames ``[frame_at(start_k), frame_at(end_k))``,
its frame count is a difference of two independently computed absolute frames, and the counts
telescope to exactly ``frame_at(total_ms)``. Because a chunk's frame count depends on its
absolute start's sub-frame phase, a chunk is always *rendered* with the same
``canonical_frames = ceil(duration_ms * fps / 1000)`` frames, and the assembly step drops the
final frame of a chunk when the absolute rule needs one fewer (:attr:`PlannedChunk.trim_frames`)
- so a chunk's fingerprint does not depend on phase, and an upstream duration change does not
re-render every chunk after it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, get_args
from uuid import UUID

from edutoon.domain.generation_settings import ORIENTATIONS
from edutoon.domain.render_animation import DEFAULT_FADE_IN_MS, DEFAULT_FADE_OUT_MS, MAX_FADE_MS
from edutoon.domain.source_storage import project_storage_prefix
from edutoon.domain.storyboard_template import StoryboardContentError, validate_scene_content
from edutoon.domain.storyboard_timing import segment_absolute_starts
from edutoon.domain.visual import visual_drawing_id

# --- production constants -----------------------------------------------------------

FPS: Final = 30
"""The production frame rate: a constant 30 fps. Every frame position in this module is derived
from it through :func:`frame_at`, never from a second copy of the number. The product's output
specification is at most 720p at 30 fps: the lessons are mostly still text and simple diagrams, so a
higher rate buys nothing visible and costs encoding time, memory and file size. (It was 60 between
the pre-API readiness pass and the deck work, and 30 before that.) Changing it is not just the
constant - the H.264 level each profile signals has to be valid for the frame size *at this rate*
(:func:`profile_level_problem`, held by a test and re-checked on every render plan and on every
finished video by :func:`check_output`), and a keyframe interval is stated in seconds
(:data:`GOP_SECONDS`), not in frames. Phase 9 stores no frame rate; only the render configuration
does."""

RENDERER_VERSION: Final = "pillow-ffmpeg/5"
"""Identifies the drawing code *and* the assembly recipe. Bump it whenever a change would
alter the pixels or the bitstream of an existing scene - every chunk fingerprint includes it,
so artifacts from an older renderer are never treated as current.

History: ``/5`` (720p, 30 fps) - the output specification became at most 1280x720 at 30 fps:
the frame sizes, the frame rate and the H.264 levels all changed, so no earlier chunk is valid.
``/4`` (60 fps) - the production frame rate went from 30 to 60, the H.264 levels were
raised to ones that are valid at that rate and the keyframe interval was re-stated in seconds.
Every chunk fingerprint already includes ``fps`` and the encoding profile, so chunks rendered at
30 fps could never have been reused; the bump makes the break explicit and independent of those
inputs. ``/3`` (Phase 11, glyph safety) - text that used a symbol Noto Sans lacks
(``-> <= >= ~= ✓`` ...) was cached with a missing-glyph box in it. Drawing now falls back to the
other bundled font, or to a drawn check/cross mark, so those pixels changed. This bump
**intentionally invalidates every previously cached chunk once** (text templates included): they
are re-rendered on the next render rather than keeping known-bad pixels."""

GOP_SECONDS: Final = 2
"""The keyframe interval, in seconds. A chunk is its own encode, so it always starts on a keyframe
whatever this is; the interval only bounds seek granularity and error propagation inside a long
scene. Two seconds is what the 30 fps profiles (60 frames) always meant."""

MAX_STORED_ERROR_CHARS: Final = 500

Quality = Literal["low", "high"]
QUALITIES: Final[frozenset[str]] = frozenset(get_args(Quality))
QUALITY_LOW: Final[Quality] = "low"
QUALITY_HIGH: Final[Quality] = "high"

Encoder = Literal["libx264", "h264_nvenc"]
ENCODERS: Final[frozenset[str]] = frozenset(get_args(Encoder))

SceneState = Literal["ready", "needs_render", "stale", "failed"]
"""One scene's render state: ``ready`` (its chunk exists and is what the last video used),
``needs_render`` (no valid chunk yet), ``stale`` (it was in the last video but has changed
since), ``failed`` (the latest attempt could not render it)."""


@dataclass(frozen=True, slots=True)
class EncodingProfile:
    """Everything that decides the *bytes* of a rendered chunk and the final MP4.

    ``scale_percent`` is the output size relative to the full frame (:data:`_FULL_SIZE`). ``low`` is
    a review render: half the linear size, so a preview costs roughly a quarter of the pixels.
    ``high`` is the deliverable: 1280x720 / 720x1280, the largest size the product produces.
    """

    quality: Quality
    scale_percent: int
    crf: int
    preset: str
    x264_profile: str
    x264_level: str
    pix_fmt: str
    gop_frames: int
    bframes: int
    x264_params: str
    audio_bitrate_kbps: int
    audio_sample_rate: int
    audio_channels: int

    def fingerprint_fields(self) -> dict[str, Any]:
        return {
            "quality": self.quality,
            "scale_percent": self.scale_percent,
            "crf": self.crf,
            "preset": self.preset,
            "profile": self.x264_profile,
            "level": self.x264_level,
            "pix_fmt": self.pix_fmt,
            "gop": self.gop_frames,
            "bframes": self.bframes,
            "x264_params": self.x264_params,
        }


# x264 tuning, MEASURED (apps/api/README.md, "Video rendering"): ``scenecut=0`` because every chunk
# is one scene and its keyframes are placed by the chunk boundaries, not by the encoder;
# ``threads=4`` because x264's default of one frame-thread per logical core (20 on the
# development machine) held ~2 GB of frames in flight per render and was no faster - with four
# threads a render peaks at about a third of the memory and finishes sooner; ``rc-lookahead=10``
# because a still-heavy scene gains nothing from looking 40 frames ahead.
X264_PARAMS: Final = "scenecut=0:threads=4:rc-lookahead=10"

# The H.264 level each profile signals, and why (ITU-T H.264 Annex A, Table A-1). A level bounds
# the macroblock (16x16) rate, the frame size and the decoded-picture buffer; a decoder is entitled
# to refuse a stream that signals a level below what its pictures actually need. At 30 fps:
#   * ``high``: 1280x720 (and 720x1280) is 80 x 45 = 3600 macroblocks a frame, 108 000 a second.
#     Level 3.0 allows 40 500 a second; level 3.1 allows 108 000 and a frame of 3600, exactly
#     enough, so 3.1 is the lowest valid level.
#   * ``low``: 640x360 is 40 x 23 = 920 macroblocks a frame, 27 600 a second. Level 2.2 allows
#     20 250; level 3.0 allows 40 500, the lowest valid level.
# :func:`profile_level_problem` recomputes this from the frame size and rate, so the table above is
# checked by a test rather than trusted, and :func:`check_output` checks the level a finished file
# really signals.

# ``bframes=0`` is deliberate: it makes the last frame of a chunk removable at assembly
# without re-encoding (nothing decodes after it), which is what lets a chunk be rendered once
# for any sub-frame phase (module docstring, "Timing"). The audio settings keep the
# narration's own mono 22.05 kHz - speech is never up-converted to stereo / 48 kHz.
PROFILES: Final[Mapping[str, EncodingProfile]] = {
    "low": EncodingProfile(
        quality="low",
        scale_percent=50,
        crf=30,
        preset="veryfast",
        x264_profile="high",
        x264_level="3.0",
        pix_fmt="yuv420p",
        gop_frames=GOP_SECONDS * FPS,
        bframes=0,
        x264_params=X264_PARAMS,
        audio_bitrate_kbps=64,
        audio_sample_rate=22_050,
        audio_channels=1,
    ),
    "high": EncodingProfile(
        quality="high",
        scale_percent=100,
        crf=20,
        preset="medium",
        x264_profile="high",
        x264_level="3.1",
        pix_fmt="yuv420p",
        gop_frames=GOP_SECONDS * FPS,
        bframes=0,
        x264_params=X264_PARAMS,
        audio_bitrate_kbps=96,
        audio_sample_rate=22_050,
        audio_channels=1,
    ),
}

# The full-quality frame size - 720p, the product's ceiling; ``low`` scales it by ``scale_percent``.
_FULL_SIZE: Final[Mapping[str, tuple[int, int]]] = {
    "horizontal": (1280, 720),
    "vertical": (720, 1280),
}

# Tolerance the finished video's stream durations must meet against the measured narration
# (module docstring, "Timing"): one frame of video quantisation plus the AAC encoder's own
# frame granularity (1024 samples = ~46 ms at 22.05 kHz). Measured, not assumed - see
# apps/api/README.md, "Video rendering".
DURATION_TOLERANCE_MS: Final = 120

# --- benchmark-derived sizing (MEASURED on the development machine; see the README) --------
# Bytes of finished video per second of output, per quality; used only to size the disk check
# and the estimate shown before a render. MEASURED on a real 10-minute lesson (text, shapes and
# illustration placeholders): 53 KB/s at ``high`` and 18 KB/s at ``low``, at 30 fps. At 60 fps a
# 9.7 minute lesson measured 39 KB/s at ``high`` and (2.4 minutes) 15 KB/s at ``low`` - smaller,
# since a static frame is nearly free - so the figures below were left as they were: rounded up by
# half again, since busier content (real photographs, once illustrations exist) will need more.
ESTIMATED_VIDEO_BYTES_PER_SECOND: Final[Mapping[str, int]] = {"low": 27_000, "high": 80_000}


def profile_for(quality: str) -> EncodingProfile:
    profile = PROFILES.get(quality)
    if profile is None:
        raise RenderPlanError(f"Unknown render quality {quality!r}.")
    return profile


def output_size(orientation: str, quality: str) -> tuple[int, int]:
    """The pixel size of a render: ``(1280, 720)`` / ``(720, 1280)`` at ``high``, scaled
    (and kept even, as H.264 4:2:0 requires) for ``low``."""
    if orientation not in ORIENTATIONS:
        raise RenderPlanError(f"Unknown orientation {orientation!r}.")
    width, height = _FULL_SIZE[orientation]
    percent = profile_for(quality).scale_percent
    return (width * percent // 100 // 2 * 2, height * percent // 100 // 2 * 2)


# --- H.264 levels ----------------------------------------------------------------------------

# (level_idc, MaxMBPS, MaxFS, MaxDpbMbs) from ITU-T H.264 Annex A, Table A-1, for the levels a 4:2:0
# 8-bit High-profile video can use. ``level_idc`` is ten times the level (4.2 -> 42), which is also
# how ffprobe reports it. Level 1b is left out: it has no bearing on HD video.
H264_LEVELS: Final[tuple[tuple[int, int, int, int], ...]] = (
    (10, 1_485, 99, 396),
    (11, 3_000, 396, 900),
    (12, 6_000, 396, 2_376),
    (13, 11_880, 396, 2_376),
    (20, 11_880, 396, 2_376),
    (21, 19_800, 792, 4_752),
    (22, 20_250, 1_620, 8_100),
    (30, 40_500, 1_620, 8_100),
    (31, 108_000, 3_600, 18_000),
    (32, 216_000, 5_120, 20_480),
    (40, 245_760, 8_192, 32_768),
    (41, 245_760, 8_192, 32_768),
    (42, 522_240, 8_704, 34_816),
    (50, 589_824, 22_080, 110_400),
    (51, 983_040, 36_864, 184_320),
    (52, 2_073_600, 36_864, 184_320),
)


def level_idc(level: str) -> int:
    """``"4.2"`` -> ``42``. Raises :class:`ValueError` for anything that is not ``major.minor``."""
    major, dot, minor = level.partition(".")
    if not dot or not major.isdigit() or not minor.isdigit() or len(minor) != 1:
        raise ValueError(f"Not an H.264 level: {level!r}.")
    return int(major) * 10 + int(minor)


def minimum_level_idc(width: int, height: int, fps: float) -> int:
    """The lowest H.264 ``level_idc`` whose limits a ``width`` x ``height`` picture at ``fps``
    frames a second fits, from Table A-1: the macroblock rate (``MaxMBPS``), the frame size
    (``MaxFS``), the picture width and height (each at most ``sqrt(8 * MaxFS)`` macroblocks) and
    room in the decoded-picture buffer for at least one whole picture (``MaxDpbMbs``). A frame is
    counted in whole macroblocks, so 1080 lines is 68 of them (1088). Raises :class:`ValueError`
    for a picture nothing in the table can hold."""
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("width, height and fps must be positive.")
    mb_width, mb_height = -(-width // 16), -(-height // 16)
    frame_mbs = mb_width * mb_height
    mb_rate = frame_mbs * fps
    for idc, max_mbps, max_fs, max_dpb in H264_LEVELS:
        if (
            mb_rate <= max_mbps
            and frame_mbs <= max_fs
            and frame_mbs <= max_dpb
            and mb_width * mb_width <= 8 * max_fs
            and mb_height * mb_height <= 8 * max_fs
        ):
            return idc
    raise ValueError(f"No H.264 level can hold {width}x{height} at {fps} fps.")


def profile_level_problem(
    profile: EncodingProfile, width: int, height: int, fps: float
) -> str | None:
    """Why ``profile``'s signalled level is not valid for a ``width`` x ``height`` picture at
    ``fps`` (a sentence for a log or a test failure), or ``None`` if it is."""
    try:
        signalled = level_idc(profile.x264_level)
        required = minimum_level_idc(width, height, fps)
    except ValueError as exc:
        return str(exc)
    if signalled < required:
        return (
            f"level {profile.x264_level} is too low for {width}x{height} at {fps:g} fps "
            f"(the lowest valid level is {required / 10:.1f})"
        )
    return None


class RenderPlanError(Exception):
    """The storyboard cannot be turned into a render plan. The message is written for the
    project's owner and shown to them verbatim, so it names what to do about it."""


# --- frame mapping ---------------------------------------------------------------------


def frame_at(t_ms: int, fps: int = FPS) -> int:
    """The frame index of the absolute position ``t_ms``: ``round(t_ms * fps / 1000)``.

    Computed in integers (round half up), never from a float and never from an earlier frame
    count - so ``frame_at(t)`` is the same number however it is reached, and rounding cannot
    accumulate. Raises :class:`ValueError` for a negative ``t_ms``.
    """
    if t_ms < 0:
        raise ValueError("t_ms must not be negative.")
    return (t_ms * fps + 500) // 1000


def canonical_frames(duration_ms: int, fps: int = FPS) -> int:
    """The number of frames a chunk of ``duration_ms`` is *rendered* with: the ceiling of its
    true length in frames. The absolute rule needs either this many or one fewer."""
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive.")
    return (duration_ms * fps + 999) // 1000


def sample_at(t_ms: int, sample_rate: int) -> int:
    """The audio sample index of the absolute position ``t_ms`` (round half up)."""
    if t_ms < 0:
        raise ValueError("t_ms must not be negative.")
    return (t_ms * sample_rate + 500) // 1000


# --- inputs ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SceneInput:
    scene_index: int
    start_ms: int
    duration_ms: int
    template: str
    content: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SegmentInput:
    """One script segment as the renderer sees it: its measured narration and its scenes.
    ``audio_content_hash`` is the narration's own fingerprint (Phase 8)."""

    segment_id: str
    segment_index: int
    audio_duration_ms: int
    audio_content_hash: str
    scenes: Sequence[SceneInput]
    # The topic the segment belongs to (any stable string, e.g. the topic id), or ``None``. Two
    # consecutive scenes with the same key are joined by a straight cut; where the key changes
    # (and at the start and end of the video) the scene fades. ``None`` compares equal to ``None``,
    # so a script with no topics is one continuous group.
    topic_key: str | None = None


# --- the plan -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedChunk:
    """One scene's render chunk.

    ``duration_ms`` is the scene's own duration, plus the duration of any zero-scene segment
    (too short for a scene of its own) that immediately follows it, so the picture never has
    a gap while that narration plays. ``start_frame`` / ``end_frame`` are absolute
    (:func:`frame_at` of the chunk's absolute start / end); ``frame_count`` is their
    difference. The chunk is rendered with ``canonical_frame_count`` frames and the assembly
    keeps only ``frame_count`` of them (``trim_frames`` is 0 or 1).
    """

    position: int
    segment_id: str
    segment_index: int
    scene_index: int
    template: str
    content: Mapping[str, Any]
    duration_ms: int
    start_ms: int
    end_ms: int
    start_frame: int
    end_frame: int
    canonical_frame_count: int
    chunk_hash: str
    # How long the scene fades in / out at its edges, in milliseconds; 0 is a straight cut. A
    # scene fades in only if it is the first of the video or opens a new topic, and fades out only
    # if it is the last or closes one. Both are part of the chunk's fingerprint.
    enter_fade_ms: int = 0
    exit_fade_ms: int = 0

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame

    @property
    def trim_frames(self) -> int:
        return self.canonical_frame_count - self.frame_count


@dataclass(frozen=True, slots=True)
class PlannedNarration:
    """One segment's narration placed on the timeline (audio track assembly)."""

    segment_id: str
    segment_index: int
    start_ms: int
    duration_ms: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class RenderPlan:
    orientation: str
    quality: str
    encoder: str
    width: int
    height: int
    fps: int
    total_ms: int
    total_frames: int
    chunks: tuple[PlannedChunk, ...]
    narration: tuple[PlannedNarration, ...]
    output_hash: str = field(default="")

    @property
    def chunk_hashes(self) -> tuple[str, ...]:
        return tuple(chunk.chunk_hash for chunk in self.chunks)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def chunk_fingerprint(
    *,
    template: str,
    content: Mapping[str, Any],
    duration_ms: int,
    orientation: str,
    quality: str,
    encoder: str,
    fps: int = FPS,
    enter_fade_ms: int = DEFAULT_FADE_IN_MS,
    exit_fade_ms: int = DEFAULT_FADE_OUT_MS,
) -> str:
    """The fingerprint of one chunk: a hash of exactly the inputs that decide its bytes.

    Deliberately absent: the chunk's position, its absolute start, its segment id and the
    narration - none of them change what is drawn, so a chunk that merely moves keeps its
    fingerprint (and is reused), and two identical scenes share one artifact.

    Present for an illustrated scene, on top of its content (which holds any visual): the
    drawing versions of the code that draws it (``domain.visual.visual_drawing_id``). Present
    for every scene: how it fades (or cuts) at each edge, which decides its first and last frames.
    """
    profile = profile_for(quality)
    inputs: dict[str, Any] = {
        "renderer": RENDERER_VERSION,
        "template": template,
        "content": content,
        "duration_ms": duration_ms,
        "canonical_frames": canonical_frames(duration_ms, fps),
        "orientation": orientation,
        "size": output_size(orientation, quality),
        "fps": fps,
        "encoder": encoder,
        "encoding": profile.fingerprint_fields(),
        # How the scene joins its neighbours: the fade at each edge (0 = a straight cut). A cut
        # edge carries no fade length, so tuning the fades re-renders only the scenes that fade.
        "transition": {"in": enter_fade_ms, "out": exit_fade_ms},
    }
    # An illustrated scene (which may carry a procedural visual, itself part of ``content``) also
    # depends on the drawing code that draws it - the versions of exactly those parts, so a change
    # to one form's layout re-renders only that form's scenes. Text-only templates have none, and
    # keep the very fingerprint they always had.
    drawing = visual_drawing_id(template, content)
    if drawing is not None:
        inputs["visual"] = drawing
    return _sha256(inputs)


def output_fingerprint(
    *,
    chunk_hashes: Sequence[str],
    narration: Sequence[PlannedNarration],
    orientation: str,
    quality: str,
    encoder: str,
    fps: int = FPS,
) -> str:
    """The fingerprint of the finished video: the ordered chunks plus the narration."""
    profile = profile_for(quality)
    return _sha256(
        {
            "renderer": RENDERER_VERSION,
            "orientation": orientation,
            "quality": quality,
            "encoder": encoder,
            "fps": fps,
            "audio": [
                profile.audio_bitrate_kbps,
                profile.audio_sample_rate,
                profile.audio_channels,
            ],
            "chunks": list(chunk_hashes),
            "narration": [[n.content_hash, n.start_ms, n.duration_ms] for n in narration],
        }
    )


def _validate_segment(segment: SegmentInput) -> None:
    where = f"Section {segment.segment_index + 1}"
    if segment.audio_duration_ms <= 0:
        raise RenderPlanError(f"{where} has no measured narration yet - narrate the script first.")
    scenes = list(segment.scenes)
    if not scenes:
        return
    if [scene.scene_index for scene in scenes] != list(range(len(scenes))):
        raise RenderPlanError(
            f"{where}'s scenes are out of order or incomplete - regenerate the storyboard."
        )
    for scene in scenes:
        if scene.duration_ms <= 0:
            raise RenderPlanError(
                f"{where} has a scene with no duration - regenerate the storyboard."
            )
        try:
            validate_scene_content(scene.template, scene.content)
        except StoryboardContentError as exc:
            raise RenderPlanError(
                f"{where} has a scene that no longer meets its template's contract ({exc}) - "
                "regenerate the storyboard."
            ) from exc
    # Scenes within a segment must tile it exactly: no gap, no overlap, no drift.
    cursor = scenes[0].start_ms
    for scene in scenes:
        if scene.start_ms != cursor:
            raise RenderPlanError(
                f"{where}'s scenes have a gap or overlap - regenerate the storyboard."
            )
        cursor += scene.duration_ms
    if sum(scene.duration_ms for scene in scenes) != segment.audio_duration_ms:
        raise RenderPlanError(
            f"{where}'s scenes no longer add up to its narration - regenerate the storyboard."
        )


def build_render_plan(
    segments: Sequence[SegmentInput],
    *,
    orientation: str,
    quality: str,
    encoder: str,
    fps: int = FPS,
    fade_in_ms: int = DEFAULT_FADE_IN_MS,
    fade_out_ms: int = DEFAULT_FADE_OUT_MS,
) -> RenderPlan:
    """The render plan for a whole script.

    Absolute positions come from the *current* narration durations (an exact prefix sum,
    never the scenes' stored ``start_ms``, which a later re-narration of an earlier segment
    can leave out of date). Raises :class:`RenderPlanError`, with an actionable message, when
    a prerequisite is missing or a scene is invalid; nothing expensive has happened by then.
    """
    if encoder not in ENCODERS:
        raise RenderPlanError(f"Unknown encoder {encoder!r}.")
    for name, value in (("fade-in", fade_in_ms), ("fade-out", fade_out_ms)):
        if not 0 <= value <= MAX_FADE_MS:
            raise RenderPlanError(f"The {name} must be between 0 and {MAX_FADE_MS} milliseconds.")
    width, height = output_size(orientation, quality)
    level_problem = profile_level_problem(profile_for(quality), width, height, fps)
    if level_problem is not None:
        # A configuration fault, caught before any expensive work: a file signalling the wrong
        # level can be refused by a player that would have played it had it been signalled right.
        raise RenderPlanError(f"The {quality} encoding profile is misconfigured: {level_problem}.")
    if not segments:
        raise RenderPlanError("This script has no sections to render.")

    ordered = sorted(segments, key=lambda segment: segment.segment_index)
    for segment in ordered:
        _validate_segment(segment)

    # (segment, scene, duration) pieces in order; a zero-scene segment's narration is absorbed
    # by the previous piece (or, at the very start, by the first) so nothing is left blank.
    pieces: list[list[Any]] = []
    leading_ms = 0
    for segment in ordered:
        if segment.scenes:
            for scene in segment.scenes:
                pieces.append([segment, scene, scene.duration_ms])
        elif pieces:
            pieces[-1][2] += segment.audio_duration_ms
        else:
            leading_ms += segment.audio_duration_ms
    if not pieces:
        raise RenderPlanError(
            "This storyboard has no scenes - every section is too short for one. "
            "Regenerate the storyboard."
        )
    pieces[0][2] += leading_ms

    chunks: list[PlannedChunk] = []
    cursor = 0
    topics = [piece[0].topic_key for piece in pieces]
    for position, (segment, scene, duration_ms) in enumerate(pieces):
        # Straight cuts inside a topic; a fade at the start and end of the video and wherever the
        # topic changes.
        opens_topic = position == 0 or topics[position] != topics[position - 1]
        closes_topic = position == len(pieces) - 1 or topics[position] != topics[position + 1]
        enter_fade_ms = fade_in_ms if opens_topic else 0
        exit_fade_ms = fade_out_ms if closes_topic else 0
        start_ms, end_ms = cursor, cursor + duration_ms
        chunks.append(
            PlannedChunk(
                position=position,
                segment_id=segment.segment_id,
                segment_index=segment.segment_index,
                scene_index=scene.scene_index,
                template=scene.template,
                content=scene.content,
                duration_ms=duration_ms,
                start_ms=start_ms,
                end_ms=end_ms,
                start_frame=frame_at(start_ms, fps),
                end_frame=frame_at(end_ms, fps),
                canonical_frame_count=canonical_frames(duration_ms, fps),
                chunk_hash=chunk_fingerprint(
                    template=scene.template,
                    content=scene.content,
                    duration_ms=duration_ms,
                    orientation=orientation,
                    quality=quality,
                    encoder=encoder,
                    fps=fps,
                    enter_fade_ms=enter_fade_ms,
                    exit_fade_ms=exit_fade_ms,
                ),
                enter_fade_ms=enter_fade_ms,
                exit_fade_ms=exit_fade_ms,
            )
        )
        cursor = end_ms

    # Narration is placed with the storyboard's own prefix sum (the read model shows the very same
    # numbers): one rule, so the timeline a reviewer sees is the timeline that renders.
    segment_starts = segment_absolute_starts([segment.audio_duration_ms for segment in ordered])
    narration: list[PlannedNarration] = [
        PlannedNarration(
            segment_id=segment.segment_id,
            segment_index=segment.segment_index,
            start_ms=start_ms,
            duration_ms=segment.audio_duration_ms,
            content_hash=segment.audio_content_hash,
        )
        for segment, start_ms in zip(ordered, segment_starts, strict=True)
    ]
    cursor = sum(segment.audio_duration_ms for segment in ordered)

    plan = RenderPlan(
        orientation=orientation,
        quality=quality,
        encoder=encoder,
        width=width,
        height=height,
        fps=fps,
        total_ms=cursor,
        total_frames=frame_at(cursor, fps),
        chunks=tuple(chunks),
        narration=tuple(narration),
    )
    return replace(
        plan,
        output_hash=output_fingerprint(
            chunk_hashes=plan.chunk_hashes,
            narration=plan.narration,
            orientation=orientation,
            quality=quality,
            encoder=encoder,
            fps=fps,
        ),
    )


# --- validity and work ---------------------------------------------------------------


def chunks_to_render(plan: RenderPlan, valid_hashes: set[str]) -> list[PlannedChunk]:
    """The chunks that still have to be rendered: those whose fingerprint has no valid
    artifact. Two scenes with the same fingerprint are rendered once (the first occurrence)."""
    seen: set[str] = set()
    work: list[PlannedChunk] = []
    for chunk in plan.chunks:
        if chunk.chunk_hash in valid_hashes or chunk.chunk_hash in seen:
            continue
        seen.add(chunk.chunk_hash)
        work.append(chunk)
    return work


def classify_scene(
    *,
    current_hash: str,
    chunk_is_ready: bool,
    last_output_hash: str | None,
    failed: bool = False,
) -> SceneState:
    """One scene's state from fingerprints alone.

    * ``failed`` - the latest attempt could not render this scene's chunk;
    * ``ready`` - its chunk exists (and, if a video has been made, it is the chunk that
      video used);
    * ``stale`` - a video has been made, this position's chunk in it was a different
      fingerprint, and the new one has not been rendered;
    * ``needs_render`` - no valid chunk and nothing earlier to be stale relative to.
    """
    if failed and not chunk_is_ready:
        return "failed"
    if chunk_is_ready:
        return "ready"
    if last_output_hash is not None and last_output_hash != current_hash:
        return "stale"
    return "needs_render"


def estimate_video_bytes(total_ms: int, quality: str) -> int:
    return (total_ms * ESTIMATED_VIDEO_BYTES_PER_SECOND[quality] + 999) // 1000


def estimate_work_bytes(plan: RenderPlan) -> int:
    """Worst-case scratch space one render needs: every chunk on disk once, the assembled
    video, and the assembled narration WAV, with headroom. Used by the pre-flight disk check."""
    video = estimate_video_bytes(plan.total_ms, plan.quality)
    wav = plan.total_ms * 22_050 * 2 // 1000
    return video * 3 + wav * 2


# --- storage keys --------------------------------------------------------------------------


def chunk_storage_key(project_id: UUID, orientation: str, quality: str, chunk_hash: str) -> str:
    """Where a rendered chunk lives: inside its project's own prefix, addressed by its
    fingerprint. Deterministic, so re-rendering the same chunk overwrites the same object, and
    built only from the project id and values this module validates - never from client input."""
    if orientation not in ORIENTATIONS or quality not in QUALITIES:
        raise RenderPlanError("Unknown render configuration.")
    if not chunk_hash.isalnum():
        raise RenderPlanError("Invalid chunk fingerprint.")
    prefix = project_storage_prefix(project_id)
    return f"{prefix}renders/chunks/{orientation}-{quality}/{chunk_hash}.mp4"


def output_storage_key(project_id: UUID, script_id: UUID, orientation: str, quality: str) -> str:
    """Where the finished video for one ``(script, orientation, quality)`` lives."""
    if orientation not in ORIENTATIONS or quality not in QUALITIES:
        raise RenderPlanError("Unknown render configuration.")
    return f"{project_storage_prefix(project_id)}renders/{script_id}/{orientation}-{quality}.mp4"


# --- verifying a finished video --------------------------------------------------------------


def check_output(summary: Mapping[str, Any], plan: RenderPlan) -> list[str]:
    """What is wrong with a finished video (an empty list means it is right).

    ``summary`` is the ffprobe reduction a renderer reports (``MediaProbe.summary()``). Every
    check is objective: the codecs, the size, the frame rate, the exact frame count the plan
    calls for, that both streams run for the narration's length (within
    :data:`DURATION_TOLERANCE_MS`) and that it can start playing before it has downloaded.
    """
    profile = profile_for(plan.quality)
    problems: list[str] = []

    def expect(name: str, actual: Any, wanted: Any) -> None:
        if actual != wanted:
            problems.append(f"{name} is {actual!r}, expected {wanted!r}")

    expect("video codec", summary.get("video_codec"), "h264")
    expect("H.264 profile", summary.get("video_profile"), profile.x264_profile.capitalize())
    expect("H.264 level", summary.get("video_level"), level_idc(profile.x264_level))
    expect("audio codec", summary.get("audio_codec"), "aac")
    expect("width", summary.get("width"), plan.width)
    expect("height", summary.get("height"), plan.height)
    expect("frame rate", summary.get("fps"), float(plan.fps))
    expect("frame count", summary.get("video_frames"), plan.total_frames)
    expect("pixel format", summary.get("pix_fmt"), profile.pix_fmt)
    expect("sample rate", summary.get("sample_rate"), profile.audio_sample_rate)
    expect("channels", summary.get("channels"), profile.audio_channels)
    if summary.get("constant_frame_rate") is not True:
        problems.append("the video is not constant frame rate")
    # Independent of what was asked for: the level the file signals must be valid for the pictures
    # it actually holds, or a decoder is entitled to refuse it (a level 4.0 file at 60 fps).
    signalled, width, height, rate = (
        summary.get("video_level"),
        summary.get("width"),
        summary.get("height"),
        summary.get("fps"),
    )
    if (
        isinstance(signalled, int)
        and isinstance(width, int)
        and isinstance(height, int)
        and isinstance(rate, int | float)
        and width > 0
        and height > 0
        and rate > 0
    ):
        try:
            required = minimum_level_idc(width, height, rate)
        except ValueError as exc:
            problems.append(str(exc))
        else:
            if signalled < required:
                problems.append(
                    f"H.264 level {signalled / 10:.1f} is too low for {width}x{height} at "
                    f"{rate:g} fps (the lowest valid level is {required / 10:.1f})"
                )
    if summary.get("faststart") is not True:
        problems.append("the index is not at the start of the file")
    for name in ("video_duration_ms", "audio_duration_ms", "container_duration_ms"):
        value = summary.get(name)
        if not isinstance(value, int) or abs(value - plan.total_ms) > DURATION_TOLERANCE_MS:
            problems.append(
                f"{name} is {value!r}, expected {plan.total_ms} +/- {DURATION_TOLERANCE_MS}"
            )
    return problems
