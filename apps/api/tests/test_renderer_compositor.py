"""The Pillow scene compositor: every template, both orientations, text alone.

These draw real frames (no FFmpeg involved) and check what a viewer would notice: nothing is
blank, nothing runs off the edge, an illustration slot shows a deliberate placeholder, the
animation dips to the background at the start and end of a scene, and drawing is deterministic.
"""

from __future__ import annotations

import numpy as np
import pytest

from edutoon.domain.storyboard_template import TEMPLATES, validate_scene_content
from edutoon.providers.renderer.compositor import (
    SceneCompositor,
    background,
    fit_text,
    font,
    wrap_text,
)
from tests.render_helpers import scene_content

SIZES = {"horizontal": (960, 540), "vertical": (540, 960), "full_h": (1920, 1080)}
ILLUSTRATED = {"labelled_diagram", "split_text_illustration", "full_bleed_illustration"}
DURATION_MS = 8_000


def _compositor(template: str, orientation: str, content=None, *, size=None, duration=DURATION_MS):
    width, height = size or SIZES[orientation]
    content = content or scene_content(template, "A clear label")
    validate_scene_content(template, content)
    return SceneCompositor(
        template=template,
        content=content,
        orientation=orientation,
        width=width,
        height=height,
        duration_ms=duration,
    )


def _pixels(image) -> np.ndarray:
    return np.asarray(image.convert("RGB"))


def _differs_from_background(image) -> np.ndarray:
    bg = _pixels(background(*image.size))
    return np.any(_pixels(image) != bg, axis=2)


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_template_draws_something_readable_from_text_alone(template, orientation):
    frame = _compositor(template, orientation).frame(DURATION_MS / 2)
    changed = _differs_from_background(frame)
    # A sentence set large on an otherwise empty frame is deliberately sparse; every other
    # template fills more of it.
    floor = 0.01 if template == "split_text_illustration" else 0.04
    assert changed.mean() > floor, f"{template}/{orientation} looks blank"
    # More than a flat fill: real content has many distinct colours (text, borders, shapes).
    assert len(np.unique(_pixels(frame).reshape(-1, 3), axis=0)) > 20


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_nothing_touches_the_edge_of_the_frame(template, orientation):
    compositor = _compositor(template, orientation)
    width, height = compositor.width, compositor.height
    ring = max(4, min(width, height) // 60)
    for t in (DURATION_MS / 2, DURATION_MS * 0.9):
        changed = _differs_from_background(compositor.frame(t))
        edge = np.zeros_like(changed)
        edge[:ring, :] = edge[-ring:, :] = True
        edge[:, :ring] = edge[:, -ring:] = True
        assert not np.any(changed & edge), f"{template}/{orientation} paints into the edge at {t}"


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_a_scene_dips_to_the_background_at_both_ends(template, orientation):
    compositor = _compositor(template, orientation)
    assert not _differs_from_background(compositor.frame(0)).any()
    assert not _differs_from_background(compositor.frame(DURATION_MS)).any()


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_drawing_is_deterministic(template):
    first = _compositor(template, "horizontal")
    second = _compositor(template, "horizontal")
    for t in (0, 700, 2_345, 5_000, 7_900):
        assert first.frame(t).tobytes() == second.frame(t).tobytes(), "drawing is not deterministic"
        assert first.state_key(t) == second.state_key(t)


def test_a_still_scene_has_one_animation_state_so_it_is_drawn_once():
    compositor = _compositor("title_card", "horizontal")
    still = {compositor.state_key(t) for t in np.arange(3_000, 6_500, 33.3)}
    assert len(still) == 1
    moving = {compositor.state_key(t) for t in np.arange(0, 900, 33.3)}
    assert len(moving) > 10


def test_bullets_appear_one_after_another():
    compositor = _compositor("bullet_points", "horizontal")
    visible = []
    for t in (400, 1_200, 2_600, 4_500):
        opacities = [state[0] for state in compositor.state_key(t)]
        visible.append(sum(1 for o in opacities if o > 250))
    assert visible == sorted(visible) and visible[0] < visible[-1]


def _bright_pixels(image) -> int:
    """Text on the dark code panel: pixels much lighter than the panel itself."""
    return int((_pixels(image).astype(int).sum(axis=2) > 3 * 150).sum())


def test_code_is_revealed_line_by_line_and_formulae_left_to_right():
    code = {"content": chr(10).join(["a = 1", "b = 2", "c = a + b"]), "kind": "code"}
    compositor = _compositor("formula_or_code", "horizontal", code)
    counts = [_bright_pixels(compositor.frame(t)) for t in (900, 1_600, 2_800, 5_000)]
    assert counts == sorted(counts) and counts[0] < counts[-1]
    formula = _compositor(
        "formula_or_code", "horizontal", {"content": "E = mc^2", "kind": "formula"}
    )
    early, late = _bright_pixels(formula.frame(500)), _bright_pixels(formula.frame(5_000))
    assert early < late


def test_code_keeps_its_indentation():
    face = font(mono=True, weight=400, size=40)
    assert wrap_text("if x:\n    return y", face, 2_000) == ["if x:", "    return y"]


def test_a_word_wider_than_the_line_is_split_rather_than_overflowing():
    face = font(mono=False, weight=400, size=40)
    lines = wrap_text("Supercalifragilisticexpialidocious", face, 200)
    assert len(lines) > 1 and all(face.getlength(line) <= 200 for line in lines)


def test_text_that_cannot_fit_is_shortened_with_an_ellipsis_not_run_off_the_frame():
    fitted = fit_text("word " * 200, max_width=300, max_height=120, max_size=40, min_size=20)
    assert fitted.height <= 120 and fitted.lines[-1].endswith("…")


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_content_at_the_maximum_lengths_still_fits_the_frame(orientation):
    worst = {
        "title_card": {"title": "T" * 30 + " " + "W" * 29, "subtitle": "s" * 60 + " " + "x" * 39},
        "bullet_points": {
            "title": "W" * 60,
            "bullets": ["Wide bullet text goes here and " + "m" * 49] * 5,
        },
        "labelled_diagram": {
            "illustration": {
                "description": "D" * 100 + " " + "e" * 59,
                "subject_labels": ["l" * 30] * 3,
            },
            "labels": [
                {"text": "L" * 30, "position": p}
                for p in ("top", "left", "right", "bottom", "centre", "top")
            ],
        },
        "split_text_illustration": {
            "text": ("word " * 32).strip()[:160],
            "illustration": {"description": "d" * 160, "subject_labels": ["s" * 30] * 3},
        },
        "full_bleed_illustration": {
            "illustration": {"description": "d" * 160, "subject_labels": ["s" * 30] * 3},
            "caption": "c" * 80,
        },
        "formula_or_code": {
            "content": "\n".join(["x" * 38] * 5),
            "kind": "code",
            "label": "L" * 40,
        },
    }
    for template, content in worst.items():
        compositor = _compositor(template, orientation, content)
        ring = max(4, min(compositor.width, compositor.height) // 60)
        changed = _differs_from_background(compositor.frame(DURATION_MS * 0.6))
        edge = np.zeros_like(changed)
        edge[:ring, :] = edge[-ring:, :] = edge[:, :ring] = edge[:, -ring:] = True
        assert changed.mean() > 0.03 and not np.any(changed & edge), template


def test_the_full_size_frame_is_1920_by_1080_and_scales_from_the_same_layout():
    small = _compositor("title_card", "horizontal")
    large = _compositor("title_card", "horizontal", size=SIZES["full_h"])
    assert large.frame(3_000).size == (1920, 1080) and small.frame(3_000).size == (960, 540)
    # The same picture at two sizes: shrinking the large one gives roughly the small one.
    shrunk = large.frame(3_000).resize((960, 540))
    difference = np.abs(_pixels(shrunk).astype(int) - _pixels(small.frame(3_000)).astype(int))
    assert difference.mean() < 6


def test_vertical_and_horizontal_are_laid_out_differently_not_just_squeezed():
    for template in sorted(TEMPLATES - {"title_card"}):
        h = _compositor(template, "horizontal").arrangement
        v = _compositor(template, "vertical").arrangement
        assert h != v


@pytest.mark.parametrize(
    ("orientation", "width", "height"),
    [("horizontal", 1280, 720), ("vertical", 720, 1280), ("horizontal", 640, 360)],
)
def test_a_longest_allowed_subtitle_is_wrapped_whole_inside_the_frame(
    orientation: str, width: int, height: int
) -> None:
    """The first frame of a real video once showed a subtitle ending "... differ in their obj".
    The cut was in the stored text (decoding stopped at the slot limit), not in the compositor;
    this pins the compositor's half: a subtitle of the longest allowed length keeps every word,
    is never given an ellipsis, and every layer stays inside the frame."""
    subtitle = (
        "we will explore the concepts of investments and savings, and understand how they differ "
        "in their objectives"
    )[:100]
    scene = SceneCompositor(
        template="title_card",
        content={"title": "in this lesson", "subtitle": subtitle},
        orientation=orientation,
        width=width,
        height=height,
        duration_ms=4000,
    )
    for layer in scene.layers:
        assert layer.x >= 0 and layer.y >= 0
        assert layer.x + layer.rgb.width <= width
        assert layer.y + layer.rgb.height <= height
    scale = (height if orientation == "horizontal" else width) / 1080
    fitted = fit_text(
        subtitle,
        max_width=round(width * 0.6),
        max_height=round(height * 0.26),
        max_size=round(44 * scale),
        min_size=round(24 * scale),
    )
    assert " ".join(fitted.lines) == subtitle and "…" not in "".join(fitted.lines)
    subtitle_layer = scene.layers[-1]
    assert subtitle_layer.rgb.height <= height * 0.3
    assert subtitle_layer.rgb.width <= width - 2 * 40
