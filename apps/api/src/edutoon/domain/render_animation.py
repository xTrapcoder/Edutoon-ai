"""Deterministic animation timing for scenes - pure domain logic (rule 1).

Every function here is a pure function of *local scene time* (milliseconds from the start of
the scene, itself derived from a local frame index as ``i * 1000 / fps``) and the scene's own
duration. Nothing reads a clock, a random source or the scene's absolute position, so the same
scene always draws the same pictures - and a chunk can be re-rendered, or rendered on another
worker, byte-for-byte alike. This is the one timeline: a procedural visual's reveal is scheduled
from the very same scene duration by the very same functions (:func:`item_reveal_starts`), so
nothing new can drift.

The motion vocabulary is deliberately small and educational: items **appear one after another**
(progressive bullets, sequential diagram steps), a code or formula block is **revealed left to
right**, a connector is **drawn** from its tail to its tip, and a scene **fades** at a topic
boundary. There is nothing else: no bounce, pulse, zoom, rotation, blur or continuous motion, and
no claim that a reveal is synchronised with the narration (the narration has no word timings; a
reveal is only paced evenly across the scene).

**Transitions (Phase 11).** Between two scenes of the *same topic* the cut is straight - the
next scene's content is on screen from its first frame (:func:`scene_envelope` with no fade, and
the compositor brings the scene's first layer in already fully visible). At a *topic boundary*
(and at the very start and end of the video) the scene fades: in over ``fade_in_ms``, out over
``fade_out_ms``, both tunable settings (``RENDER_FADE_IN_MS`` / ``RENDER_FADE_OUT_MS``), bounded to
``0 .. MAX_FADE_MS`` and shorter by default than the fades this module used to hard-code (450 ms
in, 300 ms out). Whether an edge is a cut or a fade is decided by ``domain.render`` from the
scenes' topics and is part of each chunk's fingerprint.
"""

from __future__ import annotations

from typing import Final

DEFAULT_FADE_IN_MS: Final = 240
DEFAULT_FADE_OUT_MS: Final = 200
MAX_FADE_MS: Final = 1500
"""The most a fade may be tuned to. A fade of 0 is a straight cut."""

ENTER_MS: Final = DEFAULT_FADE_IN_MS
EXIT_MS: Final = DEFAULT_FADE_OUT_MS
ITEM_FADE_MS: Final = 350
MIN_HOLD_MS: Final = 500
FIRST_ITEM_DELAY_MS: Final = 350
MAX_ITEM_STEP_MS: Final = 1400
MIN_ITEM_STEP_MS: Final = 250
SLIDE_PIXELS_AT_1080: Final = 28
"""How far, at the 1080-pixel reference size, an item slides while fading in."""
CONNECTOR_DRAW_MS: Final = 300
"""How long a connector takes to be drawn from its tail to its tip."""


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


def scene_envelope(
    t_ms: float, scene_duration_ms: int, enter_ms: int = ENTER_MS, exit_ms: int = EXIT_MS
) -> float:
    """The whole-scene opacity: 0 -> 1 over ``enter_ms``, 1 -> 0 over ``exit_ms``.

    ``enter_ms`` / ``exit_ms`` of 0 mean a straight cut on that edge: the scene is fully visible
    from its first frame / until its last. A scene shorter than ``enter_ms + exit_ms +
    MIN_HOLD_MS`` scales both fades down proportionally so the two never overlap into a scene
    that never reaches full opacity.
    """
    enter, exit_ = float(enter_ms), float(exit_ms)
    if enter + exit_ > 0 and scene_duration_ms < enter + exit_ + MIN_HOLD_MS:
        scale = max(scene_duration_ms - MIN_HOLD_MS, 0) / (enter + exit_)
        enter, exit_ = enter * scale, exit_ * scale
    fade_in = progress(t_ms, 0.0, enter) if enter > 0 else 1.0
    fade_out = 1.0 - progress(t_ms, scene_duration_ms - exit_, exit_) if exit_ > 0 else 1.0
    return min(fade_in, fade_out)


def item_reveal_starts(
    scene_duration_ms: int, count: int, *, exit_ms: int = EXIT_MS
) -> tuple[int, ...]:
    """When each of ``count`` items (bullets, diagram steps, labels) starts to appear.

    The first appears shortly after the scene opens; the rest follow at an even step that is
    capped (so a long scene does not drag) and floored (so a short one is not a blur), and is
    always chosen so the last item is fully visible with ``MIN_HOLD_MS`` to spare before the
    exit (``exit_ms`` long; 0 for a scene that ends in a straight cut). Integer milliseconds, so
    the schedule is exact.
    """
    if count <= 0:
        return ()
    if count == 1:
        return (min(FIRST_ITEM_DELAY_MS, max(scene_duration_ms // 4, 0)),)
    last_allowed = scene_duration_ms - exit_ms - MIN_HOLD_MS - ITEM_FADE_MS
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


def draw_fraction(t_ms: float, start_ms: float, span_ms: float = CONNECTOR_DRAW_MS) -> float:
    """How much of a connector has been drawn: 0 at ``start_ms``, all of it ``span_ms`` later."""
    return ease_out_cubic(progress(t_ms, start_ms, span_ms))


CUT_IN_SHIFT_MS: Final = FIRST_ITEM_DELAY_MS + ITEM_FADE_MS
"""How far a scene that *cuts in* (no fade-in) moves every layer's start earlier. A scene's
content normally starts arriving ``FIRST_ITEM_DELAY_MS`` after its first frame and takes
``ITEM_FADE_MS`` to arrive; moving everything earlier by both puts that first content fully in
place on the scene's very first frame. Every layer moves by the same amount, so the pacing of
everything after it is unchanged."""
