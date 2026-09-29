"""Scene transitions (Phase 11): straight cuts inside a topic, tunable fades at a topic boundary.

The plan decides, per chunk, how each edge joins its neighbour - a fade (of the configured length)
at the start and end of the video and wherever the topic changes, a straight cut everywhere else -
and that decision is part of the chunk's fingerprint. Proved here: which edges fade, that the
fade lengths are bounded and tunable, that a cut edge carries no fade length (so retuning the
fades re-renders only the scenes that fade), and that the animation functions honour a cut.
"""

from __future__ import annotations

import pytest

from edutoon.core.config import Settings
from edutoon.domain import render as r
from edutoon.domain import render_animation as anim

TITLE = ("title_card", {"title": "Intro"})
BULLETS = ("bullet_points", {"bullets": ["One", "Two"]})


def _plan(
    topics: list[str | None], *, scenes_per_segment: int = 1, distinct: bool = False, **kwargs
) -> r.RenderPlan:
    """One segment per entry of ``topics``, each with ``scenes_per_segment`` 3-second scenes.
    ``distinct`` gives every scene its own words, so no two share an artifact."""
    segments, cursor = [], 0
    for index, topic in enumerate(topics):
        scenes = [
            r.SceneInput(
                n,
                cursor + n * 3000,
                3000,
                *(
                    ("title_card", {"title": f"Scene {index}.{n}"})
                    if distinct
                    else (TITLE if n % 2 == 0 else BULLETS)
                ),
            )
            for n in range(scenes_per_segment)
        ]
        segments.append(
            r.SegmentInput(
                f"seg-{index}",
                index,
                3000 * scenes_per_segment,
                f"audio-{index}",
                scenes,
                topic_key=topic,
            )
        )
        cursor += 3000 * scenes_per_segment
    return r.build_render_plan(
        segments, orientation="horizontal", quality="high", encoder="libx264", **kwargs
    )


def _edges(plan: r.RenderPlan) -> list[tuple[int, int]]:
    return [(c.enter_fade_ms, c.exit_fade_ms) for c in plan.chunks]


IN, OUT = anim.DEFAULT_FADE_IN_MS, anim.DEFAULT_FADE_OUT_MS


# --- which edges fade --------------------------------------------------------------------------


def test_inside_a_topic_every_cut_is_straight_and_only_the_video_edges_fade():
    plan = _plan(["a", "a", "a", "a"])
    assert _edges(plan) == [(IN, 0), (0, 0), (0, 0), (0, OUT)]


def test_at_a_topic_boundary_the_last_scene_fades_out_and_the_next_fades_in():
    plan = _plan(["a", "a", "b", "b"])
    assert _edges(plan) == [(IN, 0), (0, OUT), (IN, 0), (0, OUT)]


def test_a_topic_of_one_scene_fades_at_both_edges():
    assert _edges(_plan(["a", "b", "c"])) == [(IN, OUT), (IN, OUT), (IN, OUT)]


def test_scenes_of_one_segment_are_cut_together_because_a_segment_has_one_topic():
    plan = _plan(["a", "b"], scenes_per_segment=3)
    assert _edges(plan) == [(IN, 0), (0, 0), (0, OUT), (IN, 0), (0, 0), (0, OUT)]


def test_a_script_with_no_topics_is_one_continuous_group():
    assert _edges(_plan([None, None, None])) == [(IN, 0), (0, 0), (0, OUT)]


def test_no_topic_and_a_topic_are_different_topics():
    assert _edges(_plan([None, "a"])) == [(IN, OUT), (IN, OUT)]


def test_a_topic_that_comes_back_later_is_a_new_boundary_each_time():
    assert _edges(_plan(["a", "b", "a"])) == [(IN, OUT), (IN, OUT), (IN, OUT)]


def test_a_single_scene_video_fades_in_and_out():
    assert _edges(_plan(["a"])) == [(IN, OUT)]


def test_a_segment_too_short_for_a_scene_does_not_change_the_edges_around_it():
    scenes = [r.SceneInput(0, 0, 3000, *TITLE)]
    segments = [
        r.SegmentInput("s0", 0, 3000, "a0", scenes, topic_key="a"),
        r.SegmentInput("s1", 1, 400, "a1", [], topic_key="b"),  # absorbed by the previous scene
        r.SegmentInput("s2", 2, 3000, "a2", [r.SceneInput(0, 3400, 3000, *BULLETS)], topic_key="a"),
    ]
    plan = r.build_render_plan(segments, orientation="horizontal", quality="low", encoder="libx264")
    assert _edges(plan) == [(IN, 0), (0, OUT)]  # both scenes are topic "a": one straight cut


# --- the fades are tunable and bounded ---------------------------------------------------------


def test_the_configured_fade_lengths_are_used_on_the_edges_that_fade():
    plan = _plan(["a", "b"], fade_in_ms=100, fade_out_ms=700)
    assert _edges(plan) == [(100, 700), (100, 700)]


def test_a_fade_of_zero_is_a_straight_cut_including_at_the_video_edges():
    assert _edges(_plan(["a", "b"], fade_in_ms=0, fade_out_ms=0)) == [(0, 0), (0, 0)]
    assert _edges(_plan(["a", "b"], fade_in_ms=0)) == [(0, OUT), (0, OUT)]


@pytest.mark.parametrize("bad", [-1, anim.MAX_FADE_MS + 1, 10_000])
@pytest.mark.parametrize("which", ["fade_in_ms", "fade_out_ms"])
def test_a_fade_outside_the_bound_is_refused_with_a_clear_message(which, bad):
    with pytest.raises(r.RenderPlanError, match=f"between 0 and {anim.MAX_FADE_MS}"):
        _plan(["a"], **{which: bad})


def test_the_bounds_themselves_are_allowed():
    assert _edges(_plan(["a"], fade_in_ms=0, fade_out_ms=anim.MAX_FADE_MS)) == [
        (0, anim.MAX_FADE_MS)
    ]


def test_the_defaults_are_tighter_than_the_fixed_fades_phase_10_used():
    assert (anim.DEFAULT_FADE_IN_MS, anim.DEFAULT_FADE_OUT_MS) == (240, 200)
    assert anim.DEFAULT_FADE_IN_MS < 450 and anim.DEFAULT_FADE_OUT_MS < 300
    assert anim.DEFAULT_FADE_IN_MS + anim.DEFAULT_FADE_OUT_MS < 450 + 300


def test_the_settings_default_to_the_domain_defaults_and_are_bounded():
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (settings.RENDER_FADE_IN_MS, settings.RENDER_FADE_OUT_MS) == (240, 200)
    for name in ("RENDER_FADE_IN_MS", "RENDER_FADE_OUT_MS"):
        Settings(_env_file=None, **{name: 0})  # type: ignore[call-arg]
        Settings(_env_file=None, **{name: anim.MAX_FADE_MS})  # type: ignore[call-arg]
        for bad in (-1, anim.MAX_FADE_MS + 1):
            with pytest.raises(ValueError, match=name):
                Settings(_env_file=None, **{name: bad})  # type: ignore[call-arg]


# --- the fingerprint ---------------------------------------------------------------------------


def _hashes(plan: r.RenderPlan) -> list[str]:
    return [c.chunk_hash for c in plan.chunks]


def test_how_a_scene_joins_its_neighbours_is_part_of_its_fingerprint():
    kwargs = dict(
        template="title_card",
        content=TITLE[1],
        duration_ms=3000,
        orientation="horizontal",
        quality="high",
        encoder="libx264",
    )
    cut = r.chunk_fingerprint(**kwargs, enter_fade_ms=0, exit_fade_ms=0)
    fade_in = r.chunk_fingerprint(**kwargs, enter_fade_ms=IN, exit_fade_ms=0)
    fade_out = r.chunk_fingerprint(**kwargs, enter_fade_ms=0, exit_fade_ms=OUT)
    both = r.chunk_fingerprint(**kwargs, enter_fade_ms=IN, exit_fade_ms=OUT)
    assert len({cut, fade_in, fade_out, both}) == 4


def test_a_cut_edge_carries_no_fade_length_so_retuning_touches_only_the_scenes_that_fade():
    topics = ["a", "a", "a", "b", "b"]
    before = _plan(topics, distinct=True)
    slower_in = _plan(topics, distinct=True, fade_in_ms=500)
    slower_out = _plan(topics, distinct=True, fade_out_ms=500)
    changed_in = {
        i
        for i, (a, b) in enumerate(zip(_hashes(before), _hashes(slower_in), strict=True))
        if a != b
    }
    changed_out = {
        i
        for i, (a, b) in enumerate(zip(_hashes(before), _hashes(slower_out), strict=True))
        if a != b
    }
    assert changed_in == {0, 3}  # the scenes that open a topic
    assert changed_out == {2, 4}  # the scenes that close one


def test_retuning_a_fade_re_renders_exactly_the_scenes_that_fade():
    topics = ["a", "a", "a", "b", "b", "b"]
    before = _plan(topics, distinct=True)
    after = _plan(topics, distinct=True, fade_out_ms=350)
    stale = r.chunks_to_render(after, set(before.chunk_hashes))
    assert [c.position for c in stale] == [2, 5]
    assert after.output_hash != before.output_hash


def test_moving_a_topic_boundary_changes_only_the_scenes_whose_edges_change():
    before = _plan(["a", "a", "b", "b"], distinct=True)
    after = _plan(["a", "a", "a", "b"], distinct=True)
    assert _edges(before) == [(IN, 0), (0, OUT), (IN, 0), (0, OUT)]
    assert _edges(after) == [(IN, 0), (0, 0), (0, OUT), (IN, OUT)]
    changed = {
        i for i, (x, y) in enumerate(zip(_hashes(before), _hashes(after), strict=True)) if x != y
    }
    assert changed == {1, 2, 3}  # the first scene is untouched: same edges, same picture


def test_the_video_edges_always_fade_so_adding_a_scene_at_the_end_changes_the_old_last_scene():
    before, after = _plan(["a", "a"], distinct=True), _plan(["a", "a", "a"], distinct=True)
    assert _hashes(before)[0] == _hashes(after)[0]
    assert _hashes(before)[1] != _hashes(after)[1]  # it used to fade out; now it cuts to the next


def test_only_the_transition_differs_between_otherwise_identical_scenes():
    plan = _plan(["a"] * 5)
    hashes = _hashes(plan)
    assert hashes[1] == hashes[2] == hashes[3]
    assert len({hashes[0], hashes[1], hashes[4]}) == 3


# --- the animation functions honour a cut ------------------------------------------------------


def test_with_no_fade_in_the_scene_is_fully_visible_from_its_first_frame():
    assert anim.scene_envelope(0, 4000, 0, 200) == 1.0
    assert anim.scene_envelope(0, 4000, 240, 200) == 0.0


def test_with_no_fade_out_the_scene_is_fully_visible_to_its_last_frame():
    assert anim.scene_envelope(4000, 4000, 240, 0) == 1.0
    assert anim.scene_envelope(3999.9, 4000, 240, 200) < 0.01
    assert anim.scene_envelope(4000, 4000, 240, 200) == pytest.approx(0.0, abs=1e-9)


def test_a_straight_cut_scene_is_never_dimmed_at_any_time():
    samples = [anim.scene_envelope(t, 3000, 0, 0) for t in range(0, 3001, 7)]
    assert set(samples) == {1.0}


@pytest.mark.parametrize(("enter", "exit_"), [(240, 200), (0, 200), (240, 0), (1500, 1500), (0, 0)])
@pytest.mark.parametrize("duration", [600, 900, 1500, 4000])
def test_the_envelope_stays_in_range_and_never_needs_more_time_than_the_scene_has(
    enter, exit_, duration
):
    samples = [anim.scene_envelope(t, duration, enter, exit_) for t in range(0, duration + 1, 5)]
    assert all(0.0 <= v <= 1.0 for v in samples)
    if enter + exit_ and duration >= enter + exit_ + anim.MIN_HOLD_MS:
        assert max(samples) == 1.0


def test_the_fade_lengths_are_the_ones_asked_for():
    # halfway through a 300 ms fade-out the scene is at half opacity; and likewise fading in
    assert anim.scene_envelope(4000 - 150, 4000, 0, 300) == pytest.approx(0.5)
    assert anim.scene_envelope(150, 4000, 300, 0) == pytest.approx(0.5)


def test_an_item_schedule_may_run_later_when_the_scene_ends_in_a_cut():
    with_fade = anim.item_reveal_starts(3_000, 5, exit_ms=anim.EXIT_MS)
    with_cut = anim.item_reveal_starts(3_000, 5, exit_ms=0)
    assert with_cut[-1] >= with_fade[-1]
    assert (
        with_cut[-1] + anim.ITEM_FADE_MS <= 3_000 - anim.MIN_HOLD_MS
    )  # still fully in, with a hold


def test_a_cut_in_puts_the_first_content_fully_in_place_and_keeps_all_the_pacing_after_it():
    normal_first_content = anim.FIRST_ITEM_DELAY_MS
    starts = [0.0, 150.0, float(normal_first_content), 1200.0]
    moved = [s - anim.CUT_IN_SHIFT_MS for s in starts]
    assert anim.CUT_IN_SHIFT_MS == anim.FIRST_ITEM_DELAY_MS + anim.ITEM_FADE_MS
    assert anim.item_alpha(0, moved[2]) == 1.0  # the first content is in place at t = 0
    assert [b - a for a, b in zip(moved, moved[1:], strict=False)] == [
        b - a for a, b in zip(starts, starts[1:], strict=False)
    ]


def test_a_connector_is_drawn_from_nothing_to_all_over_its_fixed_time():
    assert anim.draw_fraction(1000, 1000) == 0.0
    assert anim.draw_fraction(1000 + anim.CONNECTOR_DRAW_MS, 1000) == 1.0
    fractions = [anim.draw_fraction(t, 1000) for t in range(1000, 1400, 10)]
    assert fractions == sorted(fractions)
    assert 0.0 < anim.draw_fraction(1150, 1000) < 1.0
    assert anim.CONNECTOR_DRAW_MS == 300


def test_the_timing_is_exact_integers_with_no_drift_across_a_long_scene():
    """Reveal starts are exact integer milliseconds and depend only on the scene's own duration:
    the same on every call, whatever else is going on - there is no accumulated state."""
    for duration in (2_500, 6_001, 13_337):
        first = anim.item_reveal_starts(duration, 6)
        for _ in range(3):
            assert anim.item_reveal_starts(duration, 6) == first
        assert all(isinstance(s, int) for s in first)
        gaps = {b - a for a, b in zip(first, first[1:], strict=False)}
        assert len(gaps) == 1  # an even step: no rounding accumulates
