"""``domain.storyboard_timing`` - pure scene timing maths (Phase 9).

No I/O, no provider, no database: every case here is deterministic integer
arithmetic. Mutation targets: the exact duration sum, remainder distribution,
the effective scene-count formula and the minimum-scene-duration invariant
(spec section 25, items 1-4).
"""

from __future__ import annotations

import pytest

from edutoon.domain.storyboard_timing import (
    allocate_scene_durations,
    effective_scene_count,
    scene_timings,
    segment_absolute_starts,
)

# --- effective_scene_count -----------------------------------------------------------


def test_a_generous_segment_is_capped_at_the_maximum() -> None:
    assert (
        effective_scene_count(
            1_000_000, max_scenes_per_segment=6, min_scene_duration_ms=1_500
        )
        == 6
    )


def test_a_short_segment_gets_fewer_than_the_maximum() -> None:
    # 5000ms / 1500ms = 3 (floor), well under the cap of 6.
    assert (
        effective_scene_count(5_000, max_scenes_per_segment=6, min_scene_duration_ms=1_500) == 3
    )


def test_a_segment_too_short_for_even_one_scene_returns_zero() -> None:
    """The degenerate case (spec section 11): never squeeze a scene under the
    minimum - a segment shorter than the floor gets no scenes at all.
    """
    assert (
        effective_scene_count(1_000, max_scenes_per_segment=6, min_scene_duration_ms=1_500) == 0
    )


def test_a_segment_exactly_at_the_floor_gets_exactly_one_scene() -> None:
    assert (
        effective_scene_count(1_500, max_scenes_per_segment=6, min_scene_duration_ms=1_500) == 1
    )


def test_a_segment_one_millisecond_short_of_the_floor_gets_zero_scenes() -> None:
    assert (
        effective_scene_count(1_499, max_scenes_per_segment=6, min_scene_duration_ms=1_500) == 0
    )


@pytest.mark.parametrize(
    "measured_duration_ms,max_scenes_per_segment,min_scene_duration_ms",
    [(-1, 6, 1_500), (10_000, 0, 1_500), (10_000, 6, 0)],
)
def test_invalid_inputs_raise(
    measured_duration_ms: int, max_scenes_per_segment: int, min_scene_duration_ms: int
) -> None:
    with pytest.raises(ValueError):
        effective_scene_count(
            measured_duration_ms,
            max_scenes_per_segment=max_scenes_per_segment,
            min_scene_duration_ms=min_scene_duration_ms,
        )


# --- allocate_scene_durations ---------------------------------------------------------


def test_an_evenly_divisible_total_splits_equally() -> None:
    assert allocate_scene_durations(9_000, 3) == (3_000, 3_000, 3_000)


def test_the_documented_example_distributes_the_remainder_to_the_first_scenes() -> None:
    # 10001ms / 3 -> 3334, 3334, 3333 (spec section 10's worked example).
    assert allocate_scene_durations(10_001, 3) == (3_334, 3_334, 3_333)


def test_durations_always_sum_to_exactly_the_total() -> None:
    for total_ms, scene_count in [(10_001, 3), (1, 1), (7, 6), (1_000_003, 7), (300, 6)]:
        durations = allocate_scene_durations(total_ms, scene_count)
        assert sum(durations) == total_ms
        assert len(durations) == scene_count


def test_every_scene_gets_at_least_one_millisecond_when_total_equals_count() -> None:
    assert allocate_scene_durations(6, 6) == (1, 1, 1, 1, 1, 1)


def test_allocation_is_deterministic() -> None:
    assert allocate_scene_durations(10_001, 3) == allocate_scene_durations(10_001, 3)


def test_zero_or_negative_scene_count_raises() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        allocate_scene_durations(10_000, 0)


def test_fewer_milliseconds_than_scenes_raises() -> None:
    with pytest.raises(ValueError, match="smaller than scene_count"):
        allocate_scene_durations(5, 6)


# --- scene_timings ----------------------------------------------------------------


def test_scenes_start_back_to_back_with_no_gap_or_overlap() -> None:
    timings = scene_timings(1_000, [500, 300, 200])
    assert [(t.scene_index, t.start_ms, t.duration_ms) for t in timings] == [
        (0, 1_000, 500),
        (1, 1_500, 300),
        (2, 1_800, 200),
    ]
    # The next scene starts exactly where the previous one ends - no gap, no overlap.
    for previous, current in zip(timings, timings[1:], strict=False):
        assert current.start_ms == previous.start_ms + previous.duration_ms


def test_the_first_scene_starts_exactly_at_the_segment_start() -> None:
    (only,) = scene_timings(42_000, [5_000])
    assert only.start_ms == 42_000


def test_scene_timings_rejects_empty_durations() -> None:
    with pytest.raises(ValueError, match="not be empty"):
        scene_timings(0, [])


def test_scene_timings_rejects_a_non_positive_duration() -> None:
    with pytest.raises(ValueError, match="positive"):
        scene_timings(0, [500, 0, 200])


def test_scene_timings_rejects_a_negative_segment_start() -> None:
    with pytest.raises(ValueError, match="not be negative"):
        scene_timings(-1, [500])


# --- segment_absolute_starts --------------------------------------------------------


def test_the_first_segment_starts_at_zero() -> None:
    starts = segment_absolute_starts([5_000, 3_000, 2_000])
    assert starts[0] == 0


def test_segments_are_placed_back_to_back_with_no_gap_or_overlap() -> None:
    durations = [5_000, 3_000, 2_000]
    starts = segment_absolute_starts(durations)
    assert starts == (0, 5_000, 8_000)
    for index in range(len(durations) - 1):
        assert starts[index] + durations[index] == starts[index + 1]


def test_a_single_segment_starts_at_zero() -> None:
    assert segment_absolute_starts([12_345]) == (0,)


def test_segment_absolute_starts_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="not be empty"):
        segment_absolute_starts([])


def test_segment_absolute_starts_rejects_a_non_positive_duration() -> None:
    with pytest.raises(ValueError, match="positive"):
        segment_absolute_starts([5_000, 0, 2_000])
