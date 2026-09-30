"""The 60 fps production target: exact frame accounting for any duration, and an H.264 level that
is valid for the pictures actually encoded.

Pure functions, no database and no FFmpeg (the real-encoder half is in ``test_renderer_engine.py``
and ``scripts/verify_phase11.py``). What is held here:

* the production rate is a constant 60 and every frame position derives from it;
* a video of *any* millisecond duration has exactly ``round(ms * 60 / 1000)`` frames, with no
  cumulative drift however many scenes it is cut into - 1, 5, 10, 30 and 60 minutes give the
  round numbers the product promises (3 600 / 18 000 / 36 000 / 108 000 / 216 000);
* the H.264 level each profile signals is valid at 60 fps for both orientations, the 30 fps level
  is not, and ``check_output`` flags a finished file that signals a level too low for what it holds.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from edutoon.domain import render as r

TITLE = ("title_card", {"title": "Intro"})
BULLETS = ("bullet_points", {"bullets": ["One", "Two"]})


def _plan_of(total_ms: int, scene_ms: int, *, orientation: str = "horizontal") -> r.RenderPlan:
    """A single-segment script cut into scenes of ``scene_ms`` (the last takes the remainder)."""
    durations = [scene_ms] * (total_ms // scene_ms)
    if total_ms % scene_ms:
        durations.append(total_ms % scene_ms)
    scenes, cursor = [], 0
    for index, duration in enumerate(durations):
        template, content = (TITLE, BULLETS)[index % 2]
        scenes.append(r.SceneInput(index, cursor, duration, template, content))
        cursor += duration
    segment = r.SegmentInput("seg-0", 0, total_ms, "audio-0", scenes)
    return r.build_render_plan(
        [segment], orientation=orientation, quality="high", encoder="libx264"
    )


# --- the rate --------------------------------------------------------------------------------


def test_the_production_rate_is_a_constant_60_fps() -> None:
    assert r.FPS == 60


def test_one_second_is_sixty_frames_and_a_frame_is_never_derived_from_another() -> None:
    assert r.frame_at(1000) == 60
    assert r.canonical_frames(1000) == 60
    # 1000 ms / 60 is 16.67 ms: half a frame is 8.33 ms, so the boundary sits between 8 and 9.
    assert (r.frame_at(8), r.frame_at(9)) == (0, 1)


def test_the_keyframe_interval_is_two_seconds_at_the_production_rate() -> None:
    assert r.GOP_SECONDS == 2
    for profile in r.PROFILES.values():
        assert profile.gop_frames == 2 * r.FPS == 120


# --- exact frame counts for any duration ------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "frames"),
    [(1, 3_600), (5, 18_000), (10, 36_000), (30, 108_000), (60, 216_000)],
)
def test_the_round_durations_give_the_promised_frame_counts(minutes: int, frames: int) -> None:
    plan = _plan_of(minutes * 60_000, 6_000)
    assert plan.fps == 60
    assert plan.total_ms == minutes * 60_000
    assert plan.total_frames == frames


@pytest.mark.parametrize(
    "clock",
    [
        "1:00",
        "2:30",
        "3:00",
        "7:00",
        "11:30",
        "17:00",
        "23:45",
        "31:00",
        "37:15",
        "44:00",
        "51:30",
        "59:45",
        "60:00",
    ],
)
def test_any_duration_in_the_product_range_has_exactly_seconds_times_sixty_frames(
    clock: str,
) -> None:
    minutes, seconds = (int(part) for part in clock.split(":"))
    total_seconds = minutes * 60 + seconds
    plan = _plan_of(total_seconds * 1000, 7_000)
    assert plan.total_frames == total_seconds * 60
    assert sum(chunk.frame_count for chunk in plan.chunks) == plan.total_frames


@pytest.mark.parametrize("total_ms", [60_001, 61_234, 179_999, 754_321, 3_599_999, 3_600_000])
def test_a_duration_that_is_not_a_whole_frame_is_rounded_once_from_the_absolute_time(
    total_ms: int,
) -> None:
    plan = _plan_of(total_ms, 1_999)  # scenes that never land on a frame boundary
    assert plan.total_frames == r.frame_at(total_ms) == (total_ms * 60 + 500) // 1000
    assert sum(chunk.frame_count for chunk in plan.chunks) == plan.total_frames


def test_sixty_minutes_of_awkwardly_timed_scenes_do_not_drift_by_a_single_frame() -> None:
    plan = _plan_of(3_600_000, 1_501)
    assert len(plan.chunks) > 2_000
    assert plan.total_frames == 216_000
    for before, after in zip(plan.chunks, plan.chunks[1:], strict=False):
        assert after.start_frame == before.end_frame  # no gap, no overlap
        assert after.start_frame == r.frame_at(after.start_ms)  # absolute, never accumulated
    assert plan.chunks[-1].end_frame == 216_000


def test_both_orientations_have_the_same_frame_count() -> None:
    horizontal = _plan_of(300_000, 6_000, orientation="horizontal")
    vertical = _plan_of(300_000, 6_000, orientation="vertical")
    assert horizontal.total_frames == vertical.total_frames == 18_000


# --- H.264 level ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "fps", "level"),
    [
        (1920, 1080, 30, 40),  # what the 30 fps profile signalled, and it was right for 30
        (1920, 1080, 60, 42),  # the production target: 489 600 macroblocks a second
        (1080, 1920, 60, 42),  # vertical has the same macroblock count
        (960, 540, 60, 32),  # the low-quality review render
        (960, 540, 30, 31),
        (1920, 1080, 80, 51),  # the audit's diagnostic rate: NOT valid on 4.x
    ],
)
def test_the_minimum_h264_level_follows_table_a1(
    width: int, height: int, fps: int, level: int
) -> None:
    assert r.minimum_level_idc(width, height, fps) == level


def test_level_4_0_is_too_low_for_1080p60() -> None:
    profile = replace(r.PROFILES["high"], x264_level="4.0")
    problem = r.profile_level_problem(profile, 1920, 1080, 60)
    assert problem is not None and "too low" in problem and "4.2" in problem


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("quality", ["low", "high"])
def test_every_shipped_profile_signals_a_valid_level_at_the_production_rate(
    orientation: str, quality: str
) -> None:
    width, height = r.output_size(orientation, quality)
    assert r.profile_level_problem(r.PROFILES[quality], width, height, r.FPS) is None


def test_the_shipped_levels_are_the_lowest_valid_ones() -> None:
    """Not lower (a decoder may refuse) and not needlessly higher (a smaller device may refuse a
    stream that asks for more than it needs)."""
    for quality, profile in r.PROFILES.items():
        width, height = r.output_size("horizontal", quality)
        assert r.level_idc(profile.x264_level) == r.minimum_level_idc(width, height, r.FPS)


def test_a_misconfigured_profile_is_refused_before_any_work_is_done(monkeypatch) -> None:
    bad = replace(r.PROFILES["high"], x264_level="4.0")
    monkeypatch.setitem(r.PROFILES, "high", bad)
    with pytest.raises(r.RenderPlanError, match="misconfigured.*too low"):
        _plan_of(60_000, 6_000)


def test_level_parsing_rejects_anything_that_is_not_major_dot_minor() -> None:
    assert r.level_idc("4.2") == 42
    for bad in ("4", "4.", ".2", "42", "4.20", "four", ""):
        with pytest.raises(ValueError):
            r.level_idc(bad)


def test_a_picture_no_level_can_hold_is_reported() -> None:
    with pytest.raises(ValueError, match="No H.264 level"):
        r.minimum_level_idc(8192, 4320, 240)
    with pytest.raises(ValueError):
        r.minimum_level_idc(0, 1080, 60)


# --- check_output: a file that signals the wrong thing is refused ------------------------------


def _good_summary(plan: r.RenderPlan) -> dict:
    profile = r.profile_for(plan.quality)
    return {
        "container_duration_ms": plan.total_ms,
        "video_duration_ms": plan.total_ms,
        "audio_duration_ms": plan.total_ms,
        "width": plan.width,
        "height": plan.height,
        "fps": float(plan.fps),
        "video_frames": plan.total_frames,
        "video_codec": "h264",
        "video_profile": profile.x264_profile.capitalize(),
        "video_level": r.level_idc(profile.x264_level),
        "constant_frame_rate": True,
        "pix_fmt": profile.pix_fmt,
        "audio_codec": "aac",
        "sample_rate": profile.audio_sample_rate,
        "channels": profile.audio_channels,
        "faststart": True,
    }


def test_a_correct_60_fps_file_passes_every_check() -> None:
    plan = _plan_of(60_000, 6_000)
    assert r.check_output(_good_summary(plan), plan) == []


def test_a_file_that_signals_level_4_0_at_60_fps_is_refused() -> None:
    plan = _plan_of(60_000, 6_000)
    summary = {**_good_summary(plan), "video_level": 40}
    problems = r.check_output(summary, plan)
    assert any("H.264 level is 40, expected 42" in p for p in problems)
    assert any("too low for 1920x1080 at 60 fps" in p for p in problems)


def test_the_level_is_judged_against_the_pictures_the_file_really_holds() -> None:
    # A file that says it is level 4.2 (what we asked for) but is really 80 fps: the level it
    # signals is what we wanted, yet no level 4.x can hold 1080p80, and the rate is wrong too.
    plan = _plan_of(60_000, 6_000)
    summary = {**_good_summary(plan), "fps": 80.0}
    problems = r.check_output(summary, plan)
    assert any("frame rate is 80.0, expected 60.0" in p for p in problems)
    assert any("too low for 1920x1080 at 80 fps" in p for p in problems)


def test_a_wrong_frame_rate_wrong_profile_or_variable_frame_rate_is_refused() -> None:
    plan = _plan_of(60_000, 6_000)
    base = _good_summary(plan)
    assert any("frame rate" in p for p in r.check_output({**base, "fps": 30.0}, plan))
    assert any(
        "H.264 profile" in p for p in r.check_output({**base, "video_profile": "Main"}, plan)
    )
    assert any(
        "constant frame rate" in p
        for p in r.check_output({**base, "constant_frame_rate": False}, plan)
    )
    assert any("frame count" in p for p in r.check_output({**base, "video_frames": 3_599}, plan))
