"""Deterministic, integer-millisecond scene timing for Phase 9's Scene /
Storyboard layer - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services, and no configuration reads).

The narration audio measured in Phase 8 (``script_segment_audio.measured_duration_ms``,
via ``scripts.measured_duration_ms`` - see ``services.storyboard``, "Timing") is the
*only* source of scene timing; the LLM is never asked to decide how long a scene runs.
This module turns that measured duration into:

- :func:`effective_scene_count` - how many scenes one segment may be split into,
  never more than its measured duration can support at the configured minimum
  scene length (the "short segment" rule - see its docstring for the degenerate
  case where a segment cannot support even one scene);
- :func:`allocate_scene_durations` - an exact ``divmod`` split of a segment's
  measured duration across that many scenes, so the scenes' durations always sum
  to exactly the segment's measured duration, to the millisecond;
- :func:`segment_absolute_starts` - the whole script's segments' absolute start
  positions, as exact integer prefix sums of the *previous* segments' measured
  durations - never accumulated frame counts (rule 6 is about frames
  specifically; this is the same "no drift from repeated rounding" discipline
  one level up, in milliseconds, where every value here is already exact);
- :func:`scene_timings` - one segment's scenes' own absolute start positions,
  the same prefix-sum discipline applied one level down, offset by the
  segment's own absolute start.

Nothing here ever rounds: every duration and start is an exact integer
millisecond derived from ``divmod``/prefix sums, so
``SUM(scene.duration_ms) == measured_audio_duration_ms`` holds by construction,
not by chance. Frame numbers belong to a future rendering phase
(``frame = round(t_ms * fps / 1000)``) and are never computed here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def effective_scene_count(
    measured_duration_ms: int, *, max_scenes_per_segment: int, min_scene_duration_ms: int
) -> int:
    """How many scenes one segment's ``measured_duration_ms`` of narration may
    be split into: never more than ``max_scenes_per_segment``, and never more
    than the segment's own duration can support while keeping every scene at
    least ``min_scene_duration_ms`` long.

    This is the "short segment" rule: a segment whose measured duration is
    below ``min_scene_duration_ms`` yields ``0`` - never a scene shorter than
    the floor, and never fewer than zero scenes squeezed to fit. Callers must
    treat ``0`` as a deliberate, valid outcome (a segment this short gets no
    scenes) - see ``services.storyboard`` for how that is surfaced - never as
    an error and never as licence to call :func:`allocate_scene_durations`
    with a zero count.

    Raises :class:`ValueError` for a negative ``measured_duration_ms``, or a
    non-positive ``max_scenes_per_segment`` / ``min_scene_duration_ms``.
    """
    if measured_duration_ms < 0:
        raise ValueError("measured_duration_ms must not be negative.")
    if max_scenes_per_segment < 1:
        raise ValueError("max_scenes_per_segment must be at least 1.")
    if min_scene_duration_ms < 1:
        raise ValueError("min_scene_duration_ms must be at least 1.")

    return min(max_scenes_per_segment, measured_duration_ms // min_scene_duration_ms)


def allocate_scene_durations(total_ms: int, scene_count: int) -> tuple[int, ...]:
    """Split ``total_ms`` into exactly ``scene_count`` scene durations, using
    integer ``divmod`` only: every scene gets ``total_ms // scene_count``
    milliseconds, and the ``total_ms % scene_count`` millisecond remainder is
    handed out one millisecond at a time to the *first* ``remainder`` scenes -
    so the result is exact and reproducible, and
    ``sum(allocate_scene_durations(total_ms, n)) == total_ms`` always.

    Callers are expected to have chosen ``scene_count`` with
    :func:`effective_scene_count` first, which guarantees every returned
    duration is at least ``min_scene_duration_ms`` (given the same
    ``total_ms``): ``effective_scene_count`` never returns a count higher than
    ``total_ms // min_scene_duration_ms``, so the base share
    ``total_ms // scene_count`` is always at least that floor. This function
    does not re-check that invariant itself; it only requires
    ``total_ms >= scene_count`` (every scene gets at least one millisecond) as
    a defence-in-depth check, not the primary one.

    Raises :class:`ValueError` if ``scene_count`` is not positive, or if
    ``total_ms`` is smaller than ``scene_count`` (fewer milliseconds than
    scenes - a caller error, never a valid call: use
    :func:`effective_scene_count` first, which would have returned ``0``
    for this ``total_ms``).
    """
    if scene_count < 1:
        raise ValueError("scene_count must be at least 1.")
    if total_ms < scene_count:
        raise ValueError(
            f"total_ms ({total_ms}) is smaller than scene_count ({scene_count}); "
            "every scene would need at least one millisecond."
        )

    base, remainder = divmod(total_ms, scene_count)
    return tuple(base + (1 if index < remainder else 0) for index in range(scene_count))


@dataclass(frozen=True, slots=True)
class SceneTiming:
    scene_index: int
    start_ms: int
    duration_ms: int


def scene_timings(segment_start_ms: int, durations: Sequence[int]) -> tuple[SceneTiming, ...]:
    """One segment's scenes, each an exact prefix sum of the *previous*
    scenes' durations offset by ``segment_start_ms`` - never an accumulated,
    independently-rounded running total (every input here is already an exact
    integer millisecond, so there is nothing to round). Scene ``0`` starts
    exactly at ``segment_start_ms``; scene ``i`` starts exactly where scene
    ``i - 1`` ends, so there is never a gap or an overlap between them.

    Raises :class:`ValueError` for a negative ``segment_start_ms``, an empty
    ``durations``, or any non-positive duration in it.
    """
    if segment_start_ms < 0:
        raise ValueError("segment_start_ms must not be negative.")
    if not durations:
        raise ValueError("durations must not be empty.")
    if any(duration <= 0 for duration in durations):
        raise ValueError("Every duration must be positive.")

    timings: list[SceneTiming] = []
    cursor = segment_start_ms
    for index, duration in enumerate(durations):
        timings.append(SceneTiming(scene_index=index, start_ms=cursor, duration_ms=duration))
        cursor += duration
    return tuple(timings)


def segment_absolute_starts(segment_durations_ms: Sequence[int]) -> tuple[int, ...]:
    """The whole script's segments' absolute start positions, in narration
    order: segment ``0`` starts at ``0``, and segment ``i`` starts exactly
    where segment ``i - 1``'s *measured* audio ends - an exact prefix sum of
    the previous segments' ``script_segment_audio.measured_duration_ms``,
    never an accumulated, independently-rounded running total. There is
    therefore never a gap or an overlap between two consecutive segments'
    scene timelines.

    Raises :class:`ValueError` for an empty ``segment_durations_ms``, or any
    non-positive duration in it (a segment with no measured audio has no
    place in a timeline - callers only pass ``ready`` segments' durations).
    """
    if not segment_durations_ms:
        raise ValueError("segment_durations_ms must not be empty.")
    if any(duration <= 0 for duration in segment_durations_ms):
        raise ValueError("Every segment duration must be positive.")

    starts: list[int] = []
    cursor = 0
    for duration in segment_durations_ms:
        starts.append(cursor)
        cursor += duration
    return tuple(starts)
