"""``domain/render_animation.py`` - deterministic animation timing."""

from __future__ import annotations

import pytest

from edutoon.domain import render_animation as anim


@pytest.mark.parametrize("duration", [900, 1_500, 2_000, 5_000, 30_000])
def test_a_scene_starts_and_ends_on_the_background_and_is_fully_visible_between(duration):
    assert anim.scene_envelope(0, duration) == 0.0
    assert anim.scene_envelope(duration, duration) == pytest.approx(0.0, abs=1e-9)
    samples = [anim.scene_envelope(t, duration) for t in range(0, duration + 1, 10)]
    assert all(0.0 <= v <= 1.0 for v in samples)
    if duration >= 1_500:
        assert max(samples) == 1.0


def test_the_envelope_is_a_pure_function_of_time_and_duration():
    assert [anim.scene_envelope(t, 4_000) for t in range(0, 4_000, 37)] == [
        anim.scene_envelope(t, 4_000) for t in range(0, 4_000, 37)
    ]


@pytest.mark.parametrize("count", [1, 2, 3, 5, 6])
@pytest.mark.parametrize("duration", [1_500, 3_000, 6_000, 20_000])
def test_items_appear_in_order_and_are_all_visible_before_the_exit(count, duration):
    starts = anim.item_reveal_starts(duration, count)
    assert len(starts) == count
    assert list(starts) == sorted(starts)
    assert all(isinstance(s, int) for s in starts)
    if duration >= 3_000:
        # The last item is fully faded in with the minimum hold to spare before the fade-out.
        assert starts[-1] + anim.ITEM_FADE_MS <= duration - anim.EXIT_MS


def test_no_items_means_no_schedule():
    assert anim.item_reveal_starts(5_000, 0) == ()


def test_long_scenes_do_not_drag_the_step_out_beyond_its_cap():
    starts = anim.item_reveal_starts(60_000, 4)
    assert starts[1] - starts[0] == anim.MAX_ITEM_STEP_MS


def test_an_item_fades_and_slides_in_then_settles():
    assert anim.item_alpha(0, 500) == 0.0
    assert anim.item_alpha(500 + anim.ITEM_FADE_MS, 500) == 1.0
    assert anim.slide_offset(0, 500, 1.0) == anim.SLIDE_PIXELS_AT_1080
    assert anim.slide_offset(500 + anim.ITEM_FADE_MS, 500, 1.0) == 0
    assert anim.slide_offset(0, 500, 0.5) == anim.SLIDE_PIXELS_AT_1080 // 2


def test_a_reveal_runs_from_nothing_to_everything():
    assert anim.reveal_fraction(0, 6_000) == 0.0
    assert anim.reveal_fraction(6_000, 6_000) == 1.0
    fractions = [anim.reveal_fraction(t, 6_000) for t in range(0, 6_000, 100)]
    assert fractions == sorted(fractions)
