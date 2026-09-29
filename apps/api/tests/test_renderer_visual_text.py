"""The Pillow-backed text measurer, and the visual layout driven by the real fonts (Phase 11).

Where ``test_domain_visual_layout.py`` uses a fake fixed-width measurer, this uses the bundled
Noto Sans through the same ``TextFace`` the drawing uses - so what is measured is what is drawn,
including symbols drawn from the fallback font.
"""

from __future__ import annotations

import pytest

from edutoon.domain import visual as v
from edutoon.domain import visual_layout as vl
from edutoon.providers.renderer.compositor import TextFace
from edutoon.providers.renderer.visual_text import HEADROOM_PERCENT, PillowTextMeasurer
from tests.visual_corpus import comparison, components, cycle, flow, tree, valid_examples
from tests.visual_layout_helpers import all_text, assert_clean, region_for

MEASURER = PillowTextMeasurer()


def measure(text, *, size=36, weight=600, max_width=400, max_lines=2):
    return MEASURER.measure(
        text, size=size, weight=weight, max_width=max_width, max_lines=max_lines
    )


def test_short_text_is_one_line_with_the_faces_own_width():
    block = measure("Light energy")
    assert block is not None and block.lines == ("Light energy",)
    face = TextFace(mono=False, weight=600, size=36)
    assert block.width == round(face.getlength("Light energy"))
    assert block.height == block.pitch


def test_long_text_wraps_between_words_only():
    block = measure("Carbon dioxide enters the leaf through pores", max_width=300, max_lines=4)
    assert block is not None and len(block.lines) > 1
    assert " ".join(block.lines) == "Carbon dioxide enters the leaf through pores"
    assert block.height == block.pitch * len(block.lines)


def test_a_word_wider_than_the_line_is_not_split_it_does_not_fit():
    assert measure("Extraordinarily", max_width=120) is None


def test_more_lines_than_allowed_does_not_fit_and_is_never_truncated():
    text = "one two three four five six seven eight nine ten eleven twelve"
    assert measure(text, max_width=200, max_lines=2) is None
    assert measure(text, max_width=200, max_lines=9) is not None


def test_a_line_is_never_wider_than_the_headroom_allows():
    for width in (150, 240, 333, 480):
        block = measure("Light energy is absorbed by chlorophyll", max_width=width, max_lines=6)
        if block is not None:
            assert block.width <= width * HEADROOM_PERCENT // 100


def test_symbols_are_measured_as_they_will_be_drawn():
    plain = measure("A to B", max_width=1000)
    arrow = measure("A → B", max_width=1000)
    assert plain is not None and arrow is not None and arrow.width != plain.width


def test_repeated_measuring_gives_identical_answers():
    assert measure("The water cycle", max_width=260) == measure("The water cycle", max_width=260)


def test_blank_text_does_not_fit():
    assert measure("   ") is None


# --- the layout with real fonts ----------------------------------------------------------------


def lay(spec, orientation, size="full", short_side=1080):
    return vl.layout_visual(
        v.parse_visual(spec),
        orientation=orientation,
        region=region_for(orientation, size, short_side),
        short_side=short_side,
        measurer=MEASURER,
    )


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("short_side", [1080, 540])
def test_real_font_layouts_of_typical_visuals_are_clean(orientation, short_side):
    typical = [
        {
            "type": "process_flow",
            "title": "How a leaf makes sugar",
            "nodes": [
                {"label": "Light is absorbed", "detail": "by chlorophyll in the leaf"},
                {"label": "Water is split"},
                {"label": "Sugar is built", "detail": "from carbon dioxide"},
                {"label": "Oxygen is released"},
            ],
        },
        cycle(4),
        cycle(6),
        tree([None, 0, 0, 1, 1]),
        comparison(2, 3),
        components(4),
        components(5, "layers"),
    ]
    for spec in typical:
        layout = lay(spec, orientation, short_side=short_side)
        assert_clean(layout, region_for(orientation, "full", short_side), short_side)


def test_every_valid_example_is_clean_or_an_honest_overflow_with_real_fonts():
    clean = overflow = 0
    for _name, spec in valid_examples():
        for orientation in ("horizontal", "vertical"):
            try:
                layout = lay(spec, orientation)
            except vl.LayoutOverflow:
                overflow += 1
                continue
            assert_clean(layout, region_for(orientation))
            clean += 1
    assert clean > overflow


def test_real_font_layout_keeps_every_word_of_the_visual():
    spec = flow(3, title="Making sugar in a leaf")
    for orientation in ("horizontal", "vertical"):
        layout = lay(spec, orientation)
        assert "Making sugar in a leaf" in all_text(layout)


def test_a_symbol_in_a_label_lays_out_and_keeps_its_glyph_in_the_text():
    spec = {
        "type": "process_flow",
        "nodes": [{"label": "x ≤ 5"}, {"label": "Input → output"}],
    }
    layout = lay(spec, "horizontal")
    assert_clean(layout, region_for("horizontal"))
    assert "Input → output" in all_text(layout)
