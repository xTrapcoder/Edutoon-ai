"""The compositor draws symbols Noto Sans lacks (Phase 11 glyph safety).

Before the fix ``-> <= >= ~= ✓`` came out as the font's missing-glyph box in every template.
These draw real frames and check what a viewer would see: a real symbol, never a box, in every
text path - prose, titles, bullets, labels, captions, formulae and code - in both orientations.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from edutoon.domain import text_glyphs as g
from edutoon.domain.storyboard_template import TEMPLATES, validate_scene_content
from edutoon.providers.renderer.compositor import (
    INK,
    SceneCompositor,
    TextFace,
    background,
    fit_text,
    text_layer,
)
from tests.render_helpers import scene_content

ARROW = "→"
LESS_EQ = "≤"
GREATER_EQ = "≥"
APPROX = "≈"
NOT_EQ = "≠"
ROOT = "√"
TICK = "✓"
CROSS = "✗"
SYMBOLS = [ARROW, LESS_EQ, GREATER_EQ, APPROX, NOT_EQ, ROOT]
SIZE = 60


def _ink(image: Image.Image) -> np.ndarray:
    return np.asarray(image.getchannel("A"))


def _layer(text: str, *, mono: bool = False):
    fitted = fit_text(text, max_width=2000, max_height=200, max_size=SIZE, min_size=SIZE, mono=mono)
    return fitted, text_layer(fitted, INK)


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_a_symbol_sans_lacks_takes_real_room_and_leaves_real_ink(symbol):
    fitted, layer = _layer(f"a {symbol} b")
    plain_fitted, plain = _layer("a  b")
    assert fitted.width > plain_fitted.width
    assert _ink(layer).sum() > _ink(plain).sum()


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_the_symbol_drawn_is_the_real_glyph_of_the_fallback_font(symbol):
    """Rendered ink matches the fallback font's own glyph (bbox width and ink amount), and is
    different from the missing-glyph box."""
    face = TextFace(mono=False, weight=400, size=SIZE)
    image = Image.new("RGBA", (300, 200), (*INK, 0))
    face.draw_line(image, ImageDraw.Draw(image), 40, 40, symbol, INK)
    drawn = _ink(image)
    reference = Image.new("RGBA", (300, 200), (*INK, 0))
    ImageDraw.Draw(reference).text(
        (40, 40 + face.primary.getmetrics()[0]),
        symbol,
        font=face.fallback,
        fill=(*INK, 255),
        anchor="ls",
    )
    assert np.array_equal(drawn, _ink(reference))
    # ... and it is not the missing-glyph box the old code produced.
    old = Image.new("RGBA", (300, 200), (*INK, 0))
    ImageDraw.Draw(old).text((40, 40), symbol, font=face.primary, fill=(*INK, 255))
    assert not np.array_equal(drawn, _ink(old))


def test_a_line_with_nothing_unusual_is_drawn_exactly_as_before():
    """The fast path is the same single draw.text call: pixel-identical to the old behaviour."""
    face = TextFace(mono=False, weight=500, size=SIZE)
    text = "Light, water and CO₂ make sugar."
    new = Image.new("RGBA", (1400, 120), (*INK, 0))
    face.draw_line(new, ImageDraw.Draw(new), 10, 10, text, INK)
    old = Image.new("RGBA", (1400, 120), (*INK, 0))
    ImageDraw.Draw(old).text((10, 10), text, font=face.primary, fill=(*INK, 255))
    assert np.array_equal(np.asarray(new), np.asarray(old))
    assert face.getlength(text) == face.primary.getlength(text)


@pytest.mark.parametrize("mark", [TICK, CROSS])
def test_check_and_cross_marks_are_drawn_shapes_that_stay_in_their_advance_box(mark):
    face = TextFace(mono=False, weight=400, size=SIZE)
    image = Image.new("RGBA", (400, 200), (*INK, 0))
    face.draw_line(image, ImageDraw.Draw(image), 50, 40, f"x{mark}x", INK)
    ink = _ink(image)
    assert ink.sum() > 0
    start = 50 + face.primary.getlength("x")
    advance = face._mark_advance()  # noqa: SLF001 - the width the layout reserves for the mark
    columns = np.where(ink[:, :].max(axis=0) > 0)[0]
    between = [c for c in columns if start - 2 <= c <= start + advance + 2]
    assert len(between) > advance * 0.5, "the mark is missing or too narrow"
    # Not touching the neighbouring letters' space: the mark fits between the two x's.
    assert face.getlength(f"x{mark}x") == pytest.approx(
        2 * face.primary.getlength("x") + advance, abs=0.01
    )


def test_a_dropped_character_does_not_change_the_layout_or_leave_a_box():
    face = TextFace(mono=False, weight=400, size=SIZE)
    assert face.getlength("a\U0001f600b") == face.getlength("ab")
    with_emoji = Image.new("RGBA", (300, 120), (*INK, 0))
    without = Image.new("RGBA", (300, 120), (*INK, 0))
    face.draw_line(with_emoji, ImageDraw.Draw(with_emoji), 10, 10, "a\U0001f600b", INK)
    face.draw_line(without, ImageDraw.Draw(without), 10, 10, "ab", INK)
    assert np.array_equal(np.asarray(with_emoji), np.asarray(without))


def test_wrapping_measures_with_the_fallback_glyphs_so_a_line_never_overruns():
    text = " ".join(f"step {ARROW} next" for _ in range(8))
    fitted = fit_text(text, max_width=500, max_height=2000, max_size=SIZE, min_size=SIZE)
    assert len(fitted.lines) > 1
    assert all(fitted.face.getlength(line) <= 500 for line in fitted.lines)


def test_code_blocks_draw_arrows_with_the_mono_font_itself():
    face = TextFace(mono=True, weight=450, size=48)
    assert face.getlength(f"a {ARROW} b") == face.primary.getlength(f"a {ARROW} b")


# --- every existing text path, both orientations -----------------------------

SIZES = {"horizontal": (960, 540), "vertical": (540, 960)}
SENTENCE = (
    f"A {ARROW} B, x {LESS_EQ} 5, pH {GREATER_EQ} 7, {APPROX} 3, {NOT_EQ} 0, {ROOT}4 {TICK} {CROSS}"
)
FONT_FILES = {"NotoSans-VF.ttf": g.in_sans, "NotoSansMono-VF.ttf": g.in_mono}


def _content_with(template: str, text: str) -> dict:
    """The template's usual content with every text slot carrying ``text``."""
    content = scene_content(template, text)
    if template == "bullet_points":
        content["bullets"] = [text, text]
    if template == "labelled_diagram":
        content["labels"] = [{"text": text[:28], "position": "top"}]
    if template == "formula_or_code":
        content["content"] = text
    return content


def _build(template: str, orientation: str, content: dict) -> SceneCompositor:
    validate_scene_content(template, content)
    width, height = SIZES[orientation]
    return SceneCompositor(
        template=template,
        content=content,
        orientation=orientation,
        width=width,
        height=height,
        duration_ms=8_000,
    )


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_no_character_is_ever_drawn_with_a_font_that_lacks_it(template, orientation, monkeypatch):
    """The regression, at the last possible moment: spy on every text draw call the scene makes
    and check the font it hands each character to actually has a glyph for it - so no
    missing-glyph box can be produced, in any template, in either orientation."""
    calls: list[tuple[str, str]] = []
    original = ImageDraw.ImageDraw.text

    def spy(self, xy, text, fill=None, font=None, **kwargs):
        calls.append((text, Path(font.path).name))
        return original(self, xy, text, fill=fill, font=font, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    compositor = _build(template, orientation, _content_with(template, SENTENCE))
    compositor.frame(4_000)

    assert calls, "the scene drew no text"
    for text, font_file in calls:
        covers = FONT_FILES[font_file]
        lacking = [ch for ch in text if ch != " " and not covers(ch)]
        assert not lacking, f"{lacking!r} handed to {font_file}"


@pytest.mark.parametrize("template", ["title_card", "bullet_points", "split_text_illustration"])
def test_prose_templates_really_use_the_fallback_font_for_symbols(template, monkeypatch):
    fonts_used: set[str] = set()
    original = ImageDraw.ImageDraw.text

    def spy(self, xy, text, fill=None, font=None, **kwargs):
        fonts_used.add(Path(font.path).name)
        return original(self, xy, text, fill=fill, font=font, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    _build(template, "horizontal", _content_with(template, SENTENCE))
    assert fonts_used == {"NotoSans-VF.ttf", "NotoSansMono-VF.ttf"}


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_a_formula_with_symbols_is_drawn_and_wipes_in(orientation):
    compositor = _build(
        "formula_or_code",
        orientation,
        {"content": f"x {LESS_EQ} 5 {ARROW} y {NOT_EQ} 0", "kind": "formula", "label": "Rule"},
    )
    bg = np.asarray(background(*compositor.frame(0).size))
    assert np.any(np.asarray(compositor.frame(6_000)) != bg)
