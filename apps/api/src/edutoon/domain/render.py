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

from edutoon.domain.generation_settings import ORIENTATIONS
from edutoon.domain.storyboard_template import StoryboardContentError, validate_scene_content

# --- production constants -----------------------------------------------------------

FPS: Final = 30
"""The production frame rate. 30 rather than 25 or 24: educational content is slides and
short motions, where 30 gives visibly smoother fades and slides than 24/25 for ~17% more
frames than 25, and is a whole-number divisor of nothing awkward - 30 fps is the same in
Indian and European delivery (web/mobile playback is frame-rate agnostic), whereas 25 is a
broadcast convention this product does not target. Phase 9 stores no frame rate; only the
render configuration does."""

RENDERER_VERSION: Final = "pillow-ffmpeg/1"
"""Identifies the drawing code *and* the assembly recipe. Bump it whenever a change would
alter the pixels or the bitstream of an existing scene - every chunk fingerprint includes it,
so artifacts from an older renderer are never treated as current."""

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

    ``scale_percent`` is the output size relative to the full 1080-pixel frame. ``low`` is a
    review render: half the linear size, so a preview costs roughly a quarter of the pixels.
    ``high`` is the deliverable: 1920x1080 / 1080x1920.
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
        }


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
        x264_level="3.1",
        pix_fmt="yuv420p",
        gop_frames=60,
        bframes=0,
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
        x264_level="4.0",
        pix_fmt="yuv420p",
        gop_frames=60,
        bframes=0,
        audio_bitrate_kbps=96,
        audio_sample_rate=22_050,
        audio_channels=1,
    ),
}

# The full-quality frame size; ``low`` scales it by ``scale_percent``.
_FULL_SIZE: Final[Mapping[str, tuple[int, int]]] = {
    "horizontal": (1920, 1080),
    "vertical": (1080, 1920),
}

# Tolerance the finished video's stream durations must meet against the measured narration
# (module docstring, "Timing"): one frame of video quantisation plus the AAC encoder's own
# frame granularity (1024 samples = ~46 ms at 22.05 kHz). Measured, not assumed - see
# apps/api/README.md, "Video rendering".
DURATION_TOLERANCE_MS: Final = 120

# --- benchmark-derived sizing (MEASURED on the development machine; see the README) --------
# Bytes of finished video per second of output, per quality; used only to size the disk check
# and the estimate shown before a render. Deliberately generous (rounded up).
ESTIMATED_VIDEO_BYTES_PER_SECOND: Final[Mapping[str, int]] = {"low": 40_000, "high": 150_000}


def profile_for(quality: str) -> EncodingProfile:
    profile = PROFILES.get(quality)
    if profile is None:
        raise RenderPlanError(f"Unknown render quality {quality!r}.")
    return profile


def output_size(orientation: str, quality: str) -> tuple[int, int]:
    """The pixel size of a render: ``(1920, 1080)`` / ``(1080, 1920)`` at ``high``, scaled
    (and kept even, as H.264 4:2:0 requires) for ``low``."""
    if orientation not in ORIENTATIONS:
        raise RenderPlanError(f"Unknown orientation {orientation!r}.")
    width, height = _FULL_SIZE[orientation]
    percent = profile_for(quality).scale_percent
    return (width * percent // 100 // 2 * 2, height * percent // 100 // 2 * 2)


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
) -> str:
    """The fingerprint of one chunk: a hash of exactly the inputs that decide its bytes.

    Deliberately absent: the chunk's position, its absolute start, its segment id and the
    narration - none of them change what is drawn, so a chunk that merely moves keeps its
    fingerprint (and is reused), and two identical scenes share one artifact.
    """
    profile = profile_for(quality)
    return _sha256(
        {
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
        }
    )


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
) -> RenderPlan:
    """The render plan for a whole script.

    Absolute positions come from the *current* narration durations (an exact prefix sum,
    never the scenes' stored ``start_ms``, which a later re-narration of an earlier segment
    can leave out of date). Raises :class:`RenderPlanError`, with an actionable message, when
    a prerequisite is missing or a scene is invalid; nothing expensive has happened by then.
    """
    if encoder not in ENCODERS:
        raise RenderPlanError(f"Unknown encoder {encoder!r}.")
    width, height = output_size(orientation, quality)
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
    for position, (segment, scene, duration_ms) in enumerate(pieces):
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
                ),
            )
        )
        cursor = end_ms

    narration: list[PlannedNarration] = []
    cursor = 0
    for segment in ordered:
        narration.append(
            PlannedNarration(
                segment_id=segment.segment_id,
                segment_index=segment.segment_index,
                start_ms=cursor,
                duration_ms=segment.audio_duration_ms,
                content_hash=segment.audio_content_hash,
            )
        )
        cursor += segment.audio_duration_ms

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
