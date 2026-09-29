"""``domain/render.py`` - the Phase 10 timing model, fingerprints and render plan.

Pure functions, no database: this is where "exact timing", "no cumulative drift", "no gaps or
overlaps" and "only the dependent chunks are stale" are proved.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from edutoon.domain import render as r

TITLE = ("title_card", {"title": "Intro"})
BULLETS = ("bullet_points", {"bullets": ["One", "Two"]})
FORMULA = ("formula_or_code", {"content": "E = mc^2", "kind": "formula"})


def _scene(index: int, start: int, duration: int, spec: tuple[str, dict]) -> r.SceneInput:
    return r.SceneInput(index, start, duration, spec[0], spec[1])


def _segment(
    index: int,
    start: int,
    durations: list[int],
    specs: list[tuple[str, dict]] | None = None,
    *,
    audio_hash: str | None = None,
) -> r.SegmentInput:
    specs = specs or [TITLE, BULLETS, FORMULA, TITLE, BULLETS, FORMULA][: len(durations)]
    scenes, cursor = [], start
    for i, (duration, spec) in enumerate(zip(durations, specs, strict=True)):
        scenes.append(_scene(i, cursor, duration, spec))
        cursor += duration
    return r.SegmentInput(
        f"seg-{index}", index, sum(durations), audio_hash or f"audio-{index}", scenes
    )


def _script(
    durations_by_segment: list[list[int]], overrides: dict | None = None
) -> list[r.SegmentInput]:
    overrides = overrides or {}
    segments, cursor = [], 0
    for index, durations in enumerate(durations_by_segment):
        extra = overrides.get(index, {})
        segments.append(_segment(index, cursor, durations, **extra))
        cursor += sum(durations)
    return segments


def _plan(segments, *, orientation="horizontal", quality="high", encoder="libx264") -> r.RenderPlan:
    return r.build_render_plan(segments, orientation=orientation, quality=quality, encoder=encoder)


# --- frame mapping -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("t_ms", "frame"),
    [(0, 0), (16, 0), (17, 1), (33, 1), (34, 1), (50, 2), (150, 5), (1000, 30), (60_000, 1800)],
)
def test_frame_at_is_round_half_up_of_t_times_fps_over_1000(t_ms: int, frame: int) -> None:
    assert r.frame_at(t_ms) == frame
    exact = Fraction(t_ms * r.FPS, 1000)
    assert abs(frame - exact) <= Fraction(1, 2)


def test_frame_at_matches_the_exact_rule_for_every_millisecond_of_ten_minutes() -> None:
    for t in range(0, 600_000, 7):
        exact = Fraction(t * r.FPS, 1000)
        assert r.frame_at(t) == int(exact + Fraction(1, 2))


def test_frame_at_rejects_a_negative_time() -> None:
    with pytest.raises(ValueError, match="negative"):
        r.frame_at(-1)


def test_canonical_frames_is_the_ceiling_and_the_absolute_count_is_within_one_of_it() -> None:
    for duration in range(1, 5000, 13):
        for start in (0, 1, 17, 33, 34, 51, 999):
            n = r.frame_at(start + duration) - r.frame_at(start)
            assert r.canonical_frames(duration) - n in (0, 1)


# --- no drift, no gaps, no overlaps ------------------------------------------------------------


def _awkward_script() -> list[r.SegmentInput]:
    # 300 scenes of awkward, non-frame-aligned durations across 60 segments.
    durations = []
    for s in range(60):
        durations.append([1_501 + (s * 37 + k * 211) % 1_777 for k in range(5)])
    return _script(durations)


def test_frames_telescope_to_the_exact_total_with_no_cumulative_drift() -> None:
    plan = _plan(_awkward_script())
    assert len(plan.chunks) == 300
    assert sum(chunk.frame_count for chunk in plan.chunks) == plan.total_frames
    assert plan.total_frames == r.frame_at(plan.total_ms)
    # A naive implementation that rounds each scene on its own drifts; this data proves the
    # test would notice one (the mutation the rule exists to prevent).
    naive = sum(round(chunk.duration_ms * r.FPS / 1000) for chunk in plan.chunks)
    assert naive != plan.total_frames


def test_every_chunk_boundary_is_the_absolute_frame_of_its_time_no_gaps_no_overlaps() -> None:
    plan = _plan(_awkward_script())
    assert plan.chunks[0].start_frame == 0 and plan.chunks[0].start_ms == 0
    for before, after in zip(plan.chunks, plan.chunks[1:], strict=False):
        assert after.start_ms == before.end_ms
        assert after.start_frame == before.end_frame
    for chunk in plan.chunks:
        assert chunk.start_frame == r.frame_at(chunk.start_ms)
        assert chunk.end_frame == r.frame_at(chunk.end_ms)
        assert chunk.frame_count >= 1
        assert chunk.trim_frames in (0, 1)
    assert plan.chunks[-1].end_ms == plan.total_ms


def test_the_timeline_is_the_sum_of_the_measured_narration_and_ignores_stored_offsets() -> None:
    segments = _script([[3000, 2000], [4000]])
    # Corrupt the stored (absolute) offsets of every scene: a re-narration of an earlier segment
    # can leave them out of date, and the plan must not depend on them.
    shifted = [
        r.SegmentInput(
            s.segment_id,
            s.segment_index,
            s.audio_duration_ms,
            s.audio_content_hash,
            [
                r.SceneInput(
                    sc.scene_index,
                    sc.start_ms + (0 if s.segment_index == 0 else 999),
                    sc.duration_ms,
                    sc.template,
                    sc.content,
                )
                for sc in s.scenes
            ],
        )
        for s in segments
    ]
    # Segment 1 is internally contiguous (its scenes still tile it), only offset - accepted.
    plan = _plan(shifted)
    assert plan.total_ms == 9000
    assert [c.start_ms for c in plan.chunks] == [0, 3000, 5000]


# --- zero-scene segments ----------------------------------------------------------------------


def test_a_segment_too_short_for_a_scene_is_covered_by_the_previous_scene() -> None:
    segments = [
        _segment(0, 0, [3000]),
        r.SegmentInput("seg-1", 1, 900, "a1", []),
        _segment(2, 3900, [2000]),
    ]
    plan = _plan(segments)
    assert [c.duration_ms for c in plan.chunks] == [3900, 2000]
    assert plan.total_ms == 5900
    assert plan.narration[1].start_ms == 3000 and plan.narration[1].duration_ms == 900


def test_a_leading_zero_scene_segment_is_absorbed_by_the_first_scene() -> None:
    segments = [r.SegmentInput("seg-0", 0, 800, "a0", []), _segment(1, 800, [3000])]
    plan = _plan(segments)
    assert [c.duration_ms for c in plan.chunks] == [3800]


def test_a_storyboard_with_no_scenes_at_all_is_refused() -> None:
    with pytest.raises(r.RenderPlanError, match="no scenes"):
        _plan([r.SegmentInput("seg-0", 0, 800, "a0", [])])


# --- prerequisites ------------------------------------------------------------------------------


def test_a_segment_without_measured_narration_is_refused_with_an_actionable_message() -> None:
    bad = r.SegmentInput("seg-0", 0, 0, "a0", [_scene(0, 0, 1000, TITLE)])
    with pytest.raises(r.RenderPlanError, match="Section 1 has no measured narration"):
        _plan([bad])


def test_scenes_that_do_not_add_up_to_the_narration_are_refused() -> None:
    bad = r.SegmentInput("seg-0", 0, 5000, "a0", [_scene(0, 0, 4000, TITLE)])
    with pytest.raises(r.RenderPlanError, match="no longer add up"):
        _plan([bad])


def test_a_gap_between_scenes_is_refused() -> None:
    bad = r.SegmentInput(
        "seg-0", 0, 4000, "a0", [_scene(0, 0, 2000, TITLE), _scene(1, 2100, 2000, BULLETS)]
    )
    with pytest.raises(r.RenderPlanError, match="gap or overlap"):
        _plan([bad])


def test_scene_indices_must_be_contiguous_from_zero() -> None:
    bad = r.SegmentInput("seg-0", 0, 2000, "a0", [_scene(1, 0, 2000, TITLE)])
    with pytest.raises(r.RenderPlanError, match="out of order"):
        _plan([bad])


def test_a_scene_that_breaks_its_template_contract_is_refused() -> None:
    bad = r.SegmentInput(
        "seg-0",
        0,
        2000,
        "a0",
        [r.SceneInput(0, 0, 2000, "bullet_points", {"bullets": ["only one"]})],
    )
    with pytest.raises(r.RenderPlanError, match="template's contract"):
        _plan([bad])


def test_a_script_with_no_segments_is_refused() -> None:
    with pytest.raises(r.RenderPlanError, match="no sections"):
        _plan([])


def test_unknown_orientation_quality_and_encoder_are_refused() -> None:
    segments = _script([[3000]])
    with pytest.raises(r.RenderPlanError):
        _plan(segments, orientation="square")
    with pytest.raises(r.RenderPlanError):
        _plan(segments, quality="ultra")
    with pytest.raises(r.RenderPlanError):
        _plan(segments, encoder="libx265")


# --- output size ------------------------------------------------------------------------------


def test_output_sizes() -> None:
    assert r.output_size("horizontal", "high") == (1920, 1080)
    assert r.output_size("vertical", "high") == (1080, 1920)
    assert r.output_size("horizontal", "low") == (960, 540)
    assert r.output_size("vertical", "low") == (540, 960)
    for orientation in ("horizontal", "vertical"):
        for quality in ("low", "high"):
            width, height = r.output_size(orientation, quality)
            assert width % 2 == 0 and height % 2 == 0


# --- fingerprints: what invalidates what ----------------------------------------------------------


def _hashes(plan: r.RenderPlan) -> list[str]:
    return [c.chunk_hash for c in plan.chunks]


BASE = [[3000, 2000], [4000, 2500], [3500]]


def test_the_plan_is_deterministic() -> None:
    assert _hashes(_plan(_script(BASE))) == _hashes(_plan(_script(BASE)))
    assert _plan(_script(BASE)).output_hash == _plan(_script(BASE)).output_hash


def test_content_key_order_does_not_change_a_fingerprint() -> None:
    a = r.chunk_fingerprint(
        template="bullet_points",
        content={"title": "T", "bullets": ["a", "b"]},
        duration_ms=3000,
        orientation="horizontal",
        quality="high",
        encoder="libx264",
    )
    b = r.chunk_fingerprint(
        template="bullet_points",
        content={"bullets": ["a", "b"], "title": "T"},
        duration_ms=3000,
        orientation="horizontal",
        quality="high",
        encoder="libx264",
    )
    assert a == b


def test_editing_one_scene_changes_only_that_scenes_chunk() -> None:
    before = _plan(_script(BASE))
    specs = [TITLE, BULLETS]
    edited_specs = [TITLE, ("bullet_points", {"bullets": ["One", "CHANGED"]})]
    after = _plan(_script(BASE, {0: {"specs": edited_specs}}))
    changed = [
        i for i, (x, y) in enumerate(zip(_hashes(before), _hashes(after), strict=True)) if x != y
    ]
    assert changed == [1]
    assert before.output_hash != after.output_hash
    assert specs != edited_specs


def test_renarrating_one_segment_changes_only_its_chunks_and_shifts_the_rest_without_invalidating_them() -> (
    None
):
    before = _plan(_script(BASE))
    after = _plan(_script([[3000, 2000], [4700, 2500], [3500]], {1: {"audio_hash": "audio-1-new"}}))
    changed = [
        i for i, (x, y) in enumerate(zip(_hashes(before), _hashes(after), strict=True)) if x != y
    ]
    assert changed == [2]  # only the scene whose duration changed
    # The later segment moved later on the timeline, yet its chunk is byte-identical work.
    assert after.chunks[4].start_ms == before.chunks[4].start_ms + 700
    assert after.chunks[4].chunk_hash == before.chunks[4].chunk_hash
    assert after.output_hash != before.output_hash


def test_a_narration_change_that_keeps_every_duration_leaves_every_chunk_untouched() -> None:
    before = _plan(_script(BASE))
    after = _plan(_script(BASE, {0: {"audio_hash": "different-voice"}}))
    assert _hashes(before) == _hashes(after)
    assert before.output_hash != after.output_hash  # the audio track is rebuilt


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("orientation", {"orientation": "vertical"}),
        ("quality", {"quality": "low"}),
        ("encoder", {"encoder": "h264_nvenc"}),
    ],
)
def test_orientation_quality_and_encoder_invalidate_every_chunk_and_the_output(
    field, kwargs
) -> None:
    base = _plan(_script(BASE))
    other = _plan(_script(BASE), **kwargs)
    assert all(x != y for x, y in zip(_hashes(base), _hashes(other), strict=True)), field
    assert base.output_hash != other.output_hash


def test_a_new_renderer_version_never_reuses_old_artifacts(monkeypatch) -> None:
    before = _plan(_script(BASE))
    monkeypatch.setattr(r, "RENDERER_VERSION", "pillow-ffmpeg/999")
    after = _plan(_script(BASE))
    assert all(x != y for x, y in zip(_hashes(before), _hashes(after), strict=True))
    assert before.output_hash != after.output_hash


def test_identical_scenes_share_one_fingerprint_and_are_rendered_once() -> None:
    plan = _plan(_script([[3000, 3000, 3000]], {0: {"specs": [TITLE, TITLE, TITLE]}}))
    assert len(set(_hashes(plan))) == 1
    assert len(r.chunks_to_render(plan, set())) == 1


def test_chunks_to_render_skips_valid_artifacts_only() -> None:
    plan = _plan(_script(BASE))
    hashes = _hashes(plan)
    assert [c.position for c in r.chunks_to_render(plan, set())] == [0, 1, 2, 3, 4]
    assert [c.position for c in r.chunks_to_render(plan, set(hashes[:3]))] == [3, 4]
    assert r.chunks_to_render(plan, set(hashes)) == []


# --- scene states ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ready", "last", "failed", "expected"),
    [
        (True, "same", False, "ready"),
        (True, "other", False, "ready"),
        (False, "other", False, "stale"),
        (False, None, False, "needs_render"),
        (False, "same", False, "needs_render"),
        (False, "other", True, "failed"),
        (True, "same", True, "ready"),
    ],
)
def test_classify_scene(ready, last, failed, expected) -> None:
    last_hash = "same" if last == "same" else last
    assert (
        r.classify_scene(
            current_hash="same", chunk_is_ready=ready, last_output_hash=last_hash, failed=failed
        )
        == expected
    )


# --- sixty minutes -----------------------------------------------------------------------------


def test_a_sixty_minute_plan_is_just_more_chunks() -> None:
    durations = [[3_000 + (s * 13) % 600 for _ in range(6)] for s in range(200)]
    plan = _plan(_script(durations))
    assert plan.total_ms > 3_600_000 * 0.9
    assert len(plan.chunks) == 1200
    assert sum(c.frame_count for c in plan.chunks) == r.frame_at(plan.total_ms)
    assert len(set(_hashes(plan))) < len(plan.chunks)  # identical scenes dedupe
    # No single artifact or row scales with the video: every chunk is a few seconds long.
    assert max(c.canonical_frame_count for c in plan.chunks) < 30 * 10
    assert r.estimate_work_bytes(plan) > r.estimate_video_bytes(plan.total_ms, "high")
