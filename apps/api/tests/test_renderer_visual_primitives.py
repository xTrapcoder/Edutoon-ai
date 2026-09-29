"""``providers/renderer/visual_primitives.py`` - boxes, badges and connectors (Phase 11).

Real pictures, no FFmpeg. What is proved: connectors are tight-cropped (never a full-canvas
layer), their ends really touch the points they were given, an elbow is one connected stroke, an
arrowhead points the way the last segment goes, and everything is a pure function of its inputs.
"""

from __future__ import annotations

import numpy as np
import pytest

from edutoon.providers.renderer import visual_primitives as prim
from edutoon.providers.renderer.compositor import ACCENT, INK

FULL_CANVAS = 1920 * 1080
COLOUR = INK


def _alpha(image) -> np.ndarray:
    return np.asarray(image.getchannel("A"))


def _ink_at(image, offset, point, radius=3) -> int:
    """Total alpha within ``radius`` of ``point`` (a point in canvas coordinates)."""
    alpha = _alpha(image)
    x, y = point[0] - offset[0], point[1] - offset[1]
    window = alpha[
        max(0, y - radius) : min(alpha.shape[0], y + radius + 1),
        max(0, x - radius) : min(alpha.shape[1], x + radius + 1),
    ]
    return int(window.sum())


def _components(alpha: np.ndarray, threshold: int = 40) -> int:
    """Number of 4-connected components of pixels with alpha above ``threshold``."""
    mask = alpha > threshold
    seen = np.zeros_like(mask)
    count = 0
    for y, x in zip(*np.nonzero(mask), strict=True):
        if seen[y, x]:
            continue
        count += 1
        stack = [(y, x)]
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                inside = 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1]
                if inside and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
    return count


# --- integer maths -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "d", "want"),
    [(7, 2, 4), (-7, 2, -4), (5, 3, 2), (-5, 3, -2), (1, 3, 0), (-1, 3, 0), (6, -4, -2), (0, 5, 0)],
)
def test_division_rounds_half_away_from_zero_in_integers(n, d, want):
    assert prim._div_round(n, d) == want  # noqa: SLF001


@pytest.mark.parametrize(
    ("start", "end", "want"),
    [
        ((0, 0), (50, 0), (1024, 0)),
        ((0, 0), (-50, 0), (-1024, 0)),
        ((0, 0), (0, 9), (0, 1024)),
        ((0, 0), (0, -9), (0, -1024)),
        ((0, 0), (3, 4), (614, 819)),  # 3/5 and 4/5 in 1/1024ths
        ((10, 10), (7, 6), (-614, -819)),
    ],
)
def test_direction_is_an_exact_fixed_point_unit_vector(start, end, want):
    assert prim.direction(start, end) == want


def test_a_zero_length_segment_has_no_direction():
    with pytest.raises(ValueError, match="length"):
        prim.direction((5, 5), (5, 5))


def test_an_arrowhead_pointing_right_is_a_symmetric_triangle_behind_the_tip():
    tip, left, right = prim.arrowhead((100, 50), (1024, 0), 20, 10)
    assert tip == (100, 50)
    assert {left, right} == {(80, 60), (80, 40)}


def test_an_arrowhead_pointing_down_is_rotated_exactly():
    tip, a, b = prim.arrowhead((50, 100), (0, 1024), 20, 10)
    assert tip == (50, 100)
    assert {a, b} == {(40, 80), (60, 80)}


# --- connectors --------------------------------------------------------------------------------


def test_a_connector_is_a_tight_crop_of_what_it_draws_never_a_full_canvas_layer():
    image, offset = prim.connector_image(
        [(300, 200), (1500, 200)], stroke=8, colour=COLOUR, head_length=30, head_half_width=16
    )
    assert image.size[0] * image.size[1] < FULL_CANVAS // 20
    assert image.size[0] < 1300 and image.size[1] < 80
    assert offset[0] < 300 and offset[1] < 200


def test_the_start_and_the_tip_of_a_connector_really_touch_the_points_given():
    points = [(120, 80), (420, 80)]
    image, offset = prim.connector_image(
        points, stroke=8, colour=COLOUR, head_length=30, head_half_width=16
    )
    assert _ink_at(image, offset, points[0]) > 0
    assert _ink_at(image, offset, (points[1][0] - 2, points[1][1])) > 0
    assert _ink_at(image, offset, (points[1][0] + 12, points[1][1])) == 0  # nothing beyond the tip


@pytest.mark.parametrize(
    "end",
    [(400, 100), (0, 100), (200, 300), (200, -100), (350, 250), (50, -50), (-100, 260), (330, -60)],
    ids=["right", "left", "down", "up", "down-right", "up-left", "down-left", "up-right"],
)
def test_the_arrowhead_points_the_way_the_last_segment_goes(end):
    start = (200, 100)
    image, offset = prim.connector_image(
        [start, end], stroke=6, colour=COLOUR, head_length=34, head_half_width=18
    )
    # A head-sized neighbourhood: the head's triangle is behind its tip, so compare what lies
    # within one head-length of each end.
    near_tip = _ink_at(image, offset, end, radius=34)
    near_start = _ink_at(image, offset, start, radius=34)
    assert near_tip > near_start * 1.5, "the heavy end must be the tip"


def test_without_a_head_both_ends_have_the_same_weight():
    start, end = (100, 100), (400, 100)
    image, offset = prim.connector_image([start, end], stroke=8, colour=COLOUR)
    assert _ink_at(image, offset, start, 10) == pytest.approx(
        _ink_at(image, offset, end, 10), rel=0.1
    )


def test_an_elbow_connector_is_one_connected_stroke_from_start_to_tip():
    points = [(100, 100), (100, 220), (500, 220), (500, 330)]
    image, offset = prim.connector_image(
        points, stroke=8, colour=COLOUR, head_length=30, head_half_width=16
    )
    assert _components(_alpha(image)) == 1
    for point in (points[0], points[1], points[2]):
        assert _ink_at(image, offset, point) > 0
    assert _ink_at(image, offset, (points[3][0], points[3][1] - 3)) > 0


def test_a_leader_line_ends_in_a_dot():
    end = (300, 150)
    dotted, offset = prim.connector_image([(100, 150), end], stroke=4, colour=COLOUR, dot_radius=9)
    plain, plain_offset = prim.connector_image([(100, 150), end], stroke=4, colour=COLOUR)
    assert _ink_at(dotted, offset, end, 8) > _ink_at(plain, plain_offset, end, 8) * 2


def test_a_connector_needs_two_points():
    with pytest.raises(ValueError, match="two points"):
        prim.connector_image([(1, 1)], stroke=4, colour=COLOUR)


def test_a_connector_with_a_short_last_segment_still_draws_its_head():
    image, offset = prim.connector_image(
        [(100, 100), (110, 100)], stroke=6, colour=COLOUR, head_length=34, head_half_width=18
    )
    assert _alpha(image).max() == 255


def test_connectors_are_deterministic_pixel_for_pixel():
    args = dict(stroke=7, colour=ACCENT, head_length=28, head_half_width=15)
    points = [(90, 60), (90, 200), (410, 240)]
    first, first_offset = prim.connector_image(points, **args)
    second, second_offset = prim.connector_image(points, **args)
    assert first_offset == second_offset
    assert first.tobytes() == second.tobytes()


def test_the_colour_is_exactly_the_one_asked_for_even_on_anti_aliased_edges():
    image, _ = prim.connector_image([(50, 50), (300, 120)], stroke=16, colour=(200, 30, 60))
    rgb = np.asarray(image.convert("RGB"))
    alpha = _alpha(image)
    full = alpha == 255
    # Deep inside the stroke (every neighbour fully opaque) the colour is exactly the one asked
    # for; only the anti-aliased rim - where resampling overshoots - may tint slightly.
    interior = full.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            interior &= np.roll(np.roll(full, dy, axis=0), dx, axis=1)
    assert interior.sum() > 50
    assert (rgb[interior] == np.array([200, 30, 60])).all()


# --- boxes, badges, text, dividers -------------------------------------------------------------


def test_a_box_is_exactly_the_size_asked_for_with_transparent_corners():
    image = prim.box_image(
        300, 120, radius=30, fill=(255, 255, 255), outline=ACCENT, outline_width=3
    )
    assert image.size == (300, 120)
    alpha = _alpha(image)
    assert alpha[0, 0] < 30 and alpha[60, 150] == 255


def test_a_marker_is_a_disc_with_its_number_on_it():
    one = prim.marker_image(1, 48)
    nine = prim.marker_image(9, 48)
    assert one.size == nine.size == (48, 48)
    assert not np.array_equal(np.asarray(one), np.asarray(nine))
    rgb = np.asarray(one.convert("RGB"))
    assert (rgb[24, 4] == np.array(ACCENT)).all()  # the disc
    assert (rgb == 255).all(axis=2).sum() > 20  # the white digit


def test_text_is_drawn_exactly_as_the_layout_wrapped_it():
    image = prim.text_image(
        ["First line", "second line"], size=30, weight=500, pitch=38, colour=INK, width=400
    )
    assert image.size == (400, 76)
    assert _alpha(image).sum() > 0
    top_half = _alpha(image)[:38].sum()
    bottom_half = _alpha(image)[38:].sum()
    assert top_half > 0 and bottom_half > 0


def test_a_divider_is_a_rounded_bar():
    bar = prim.divider_image(140, 10, ACCENT)
    assert bar.size == (140, 10)
    assert _alpha(bar)[5, 70] == 255
