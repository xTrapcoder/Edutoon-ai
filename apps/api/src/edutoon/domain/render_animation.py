"""Deterministic animation timing for Phase 10 scenes - pure domain logic (rule 1).

Every function here is a pure function of *local scene time* (milliseconds from the start of
the scene, itself derived from a local frame index as ``i * 1000 / fps``) and the scene's own
duration. Nothing reads a clock, a random source or the scene's absolute position, so the same
scene always draws the same pictures - and a chunk can be re-rendered, or rendered on another
worker, byte-for-byte alike.

The motion vocabulary is deliberately small and educational: a scene **fades and slides in**,
its items **appear one after another** (progressive bullets, sequential diagram labels), a code
or formula block is **revealed left to right**, and the scene **fades out** at its end so that
consecutive scenes read as a section transition (a dip to the background) rather than a jump.
"""

from __future__ import annotations

from typing import Final

ENTER_MS: Final = 450
EXIT_MS: Final = 300
ITEM_FADE_MS: Final = 350
MIN_HOLD_MS: Final = 500
FIRST_ITEM_DELAY_MS: Final = 500
MAX_ITEM_STEP_MS: Final = 1400
MIN_ITEM_STEP_MS: Final = 250
SLIDE_PIXELS_AT_1080: Final = 28
"""How far, at the 1080-pixel reference size, an item slides while fading in."""


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def ease_out_cubic(x: float) -> float:
    x = clamp01(x)
    return 1.0 - (1.0 - x) ** 3


def progress(t_ms: float, start_ms: float, duration_ms: float) -> float:
    """0 before ``start_ms``, 1 after ``start_ms + duration_ms``, linear between."""
    if duration_ms <= 0:
        return 1.0 if t_ms >= start_ms else 0.0
    return clamp01((t_ms - start_ms) / duration_ms)


def scene_envelope(t_ms: float, scene_duration_ms: int) -> float:
    """The whole-scene opacity: 0 -> 1 over the enter, 1 -> 0 over the exit.

    A scene shorter than ``ENTER_MS + EXIT_MS`` scales both down proportionally so the two
    never overlap into a scene that never reaches full opacity.
    """
    enter, exit_ = float(ENTER_MS), float(EXIT_MS)
    if scene_duration_ms < enter + exit_ + MIN_HOLD_MS:
        scale = max(scene_duration_ms - MIN_HOLD_MS, 0) / (enter + exit_)
        enter, exit_ = enter * scale, exit_ * scale
    fade_in = progress(t_ms, 0.0, enter) if enter > 0 else 1.0
    fade_out = 1.0 - progress(t_ms, scene_duration_ms - exit_, exit_) if exit_ > 0 else 1.0
    return min(fade_in, fade_out)


def item_reveal_starts(scene_duration_ms: int, count: int) -> tuple[int, ...]:
    """When each of ``count`` items (bullets, diagram labels) starts to appear.

    The first appears shortly after the scene opens; the rest follow at an even step that is
    capped (so a long scene does not drag) and floored (so a short one is not a blur), and is
    always chosen so the last item is fully visible with ``MIN_HOLD_MS`` to spare before the
    exit. Integer milliseconds, so the schedule is exact.
    """
    if count <= 0:
        return ()
    if count == 1:
        return (min(FIRST_ITEM_DELAY_MS, max(scene_duration_ms // 4, 0)),)
    last_allowed = scene_duration_ms - EXIT_MS - MIN_HOLD_MS - ITEM_FADE_MS
    window = max(last_allowed - FIRST_ITEM_DELAY_MS, 0)
    step = max(MIN_ITEM_STEP_MS, min(MAX_ITEM_STEP_MS, window // (count - 1)))
    first = FIRST_ITEM_DELAY_MS
    if first + step * (count - 1) > max(last_allowed, first):
        # Too short for the floor step: squeeze the whole schedule into the window instead.
        step = max(window // (count - 1), 1)
    return tuple(first + step * index for index in range(count))


def item_alpha(t_ms: float, start_ms: float) -> float:
    return ease_out_cubic(progress(t_ms, start_ms, ITEM_FADE_MS))


def slide_offset(t_ms: float, start_ms: float, scale: float) -> int:
    """Vertical offset in pixels (positive = below its final place) of an item that is
    sliding up into position; 0 once it has arrived."""
    remaining = 1.0 - ease_out_cubic(progress(t_ms, start_ms, ITEM_FADE_MS))
    return round(SLIDE_PIXELS_AT_1080 * scale * remaining)


def reveal_fraction(
    t_ms: float, scene_duration_ms: int, *, lead_ms: int = FIRST_ITEM_DELAY_MS
) -> float:
    """How much of a left-to-right reveal (a code line, a formula) is shown: 0 at ``lead_ms``,
    1 after at most 2.4 s or 45% of the scene, whichever is shorter."""
    span = min(2400.0, scene_duration_ms * 0.45)
    return ease_out_cubic(progress(t_ms, float(lead_ms), max(span, 1.0)))
