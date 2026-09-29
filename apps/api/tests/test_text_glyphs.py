"""``domain/text_glyphs.py`` - which characters the bundled fonts can draw, and the run plan.

Pure functions. The defect this guards (Phase 11): Noto Sans has no glyph for ``-> <= >= ~= ✓``
and many other symbols, so they were drawn as the missing-glyph box. Every symbol below is
written as an escape so the test file cannot be damaged by an editor's encoding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edutoon.domain import text_glyphs as g

ARROW = "→"
LEFT_ARROW = "←"
LESS_EQ = "≤"
GREATER_EQ = "≥"
APPROX = "≈"
NOT_EQ = "≠"
ROOT = "√"
TICK = "✓"
HEAVY_TICK = "✔"
CROSS = "✗"
HEAVY_CROSS = "✘"
FONT_DIR = Path(__file__).resolve().parents[1] / "src/edutoon/providers/renderer/assets/fonts"

# The symbols the task named plus the ones an LLM writing a science or maths lesson reaches for.
DEFECT_SYMBOLS = [
    ARROW, LEFT_ARROW, LESS_EQ, GREATER_EQ, APPROX, NOT_EQ, ROOT, TICK,
    "↑", "↓", "↔", "⇒", "∞", "●", "○", "□", "■",
    "▲", "▶", HEAVY_TICK, CROSS, HEAVY_CROSS,
]  # fmt: skip


def _ranges_from_font(name: str) -> tuple[tuple[int, int], ...]:
    ttlib = pytest.importorskip("fontTools.ttLib")
    cmap = ttlib.TTFont(str(FONT_DIR / name)).getBestCmap()
    out: list[tuple[int, int]] = []
    for codepoint in sorted(cp for cp in cmap if cp >= 0x20):
        if out and codepoint == out[-1][1] + 1:
            out[-1] = (out[-1][0], codepoint)
        else:
            out.append((codepoint, codepoint))
    return tuple(out)


# --- the tables match the fonts ------------------------------------------------------------------


def test_the_sans_coverage_table_is_exactly_the_fonts_own_cmap() -> None:
    assert _ranges_from_font("NotoSans-VF.ttf") == g.SANS_RANGES


def test_the_mono_coverage_table_is_exactly_the_fonts_own_cmap() -> None:
    assert _ranges_from_font("NotoSansMono-VF.ttf") == g.MONO_RANGES


def test_the_defect_is_real_sans_lacks_symbols_that_mono_has() -> None:
    """If this ever fails the fonts changed; re-read the module docstring before relaxing it."""
    for symbol in (ARROW, LESS_EQ, GREATER_EQ, APPROX, NOT_EQ, ROOT):
        assert not g.in_sans(symbol), symbol
        assert g.in_mono(symbol), symbol
    assert not g.in_sans(TICK) and not g.in_mono(TICK)


# --- how each character is drawn -----------------------------------------------------------------


@pytest.mark.parametrize("symbol", [ARROW, LEFT_ARROW, LESS_EQ, GREATER_EQ, APPROX, NOT_EQ, ROOT])
def test_symbols_only_mono_has_fall_back_to_mono_inside_prose(symbol: str) -> None:
    assert g.glyph_kind(symbol) == "fallback"
    assert g.plan_runs(f"a {symbol} b") == (
        g.GlyphRun("primary", "a "),
        g.GlyphRun("fallback", symbol),
        g.GlyphRun("primary", " b"),
    )


def test_the_same_symbols_are_the_fonts_own_glyphs_inside_code() -> None:
    assert g.glyph_kind(ARROW, primary="mono") == "primary"
    assert g.plan_runs(f"x {ARROW} y", primary="mono") == (g.GlyphRun("primary", f"x {ARROW} y"),)


def test_check_and_cross_marks_are_drawn_shapes_in_both_kinds_of_text() -> None:
    for primary in ("sans", "mono"):
        assert g.glyph_kind(TICK, primary=primary) == "tick"  # type: ignore[arg-type]
        assert g.glyph_kind(HEAVY_TICK, primary=primary) == "tick"  # type: ignore[arg-type]
        assert g.glyph_kind(CROSS, primary=primary) == "cross"  # type: ignore[arg-type]
        assert g.glyph_kind(HEAVY_CROSS, primary=primary) == "cross"  # type: ignore[arg-type]


def test_every_defect_symbol_can_now_be_drawn() -> None:
    assert [s for s in DEFECT_SYMBOLS if g.glyph_kind(s) is None] == []
    assert g.is_drawable("".join(DEFECT_SYMBOLS))


def test_ordinary_text_is_one_primary_run_returned_unchanged() -> None:
    text = "Photosynthesis needs light, water and CO₂ (H₂O too)."
    assert g.plan_runs(text) == (g.GlyphRun("primary", text),)


def test_subscripts_superscripts_greek_and_currency_need_no_fallback() -> None:
    for text in ("CO₂", "x²", "αβγ π", "₹500 £5 €7", "5 °C"):
        assert g.plan_runs(text) == (g.GlyphRun("primary", text),)


def test_adjacent_marks_stay_separate_runs_so_each_is_drawn() -> None:
    kinds = [run.kind for run in g.plan_runs(f"{TICK}{CROSS}")]
    assert kinds == ["tick", "cross"]


# --- what is never drawn -----------------------------------------------------


@pytest.mark.parametrize(
    "junk",
    ["\U0001f600", "​", "‍", "‮", "️", "", "\x00", "\x1b", "­"],
    ids=["emoji", "zero-width", "joiner", "bidi-override", "variation-selector", "private-use",
         "nul", "escape", "soft-hyphen"],
)  # fmt: skip
def test_characters_nothing_can_draw_are_dropped_never_drawn_as_a_box(junk: str) -> None:
    assert g.plan_runs(f"a{junk}b") == (g.GlyphRun("primary", "ab"),)
    assert not g.is_drawable(junk)


def test_a_tab_is_a_space() -> None:
    assert g.plan_runs("a\tb") == (g.GlyphRun("primary", "a b"),)


def test_close_substitutes_are_applied_only_where_nothing_covers_the_original() -> None:
    assert g.plan_runs("⇄") == (g.GlyphRun("fallback", "↔"),)  # arrows over each other
    assert g.plan_runs("∑") == (g.GlyphRun("primary", "Σ"),)  # n-ary sum -> sigma


def test_every_substitute_target_is_itself_drawable_or_empty() -> None:
    for original, target in g.SUBSTITUTES.items():
        assert g.glyph_kind(original) is None, f"{original!r} is covered; no substitute needed"
        assert all(g.glyph_kind(ch) is not None for ch in target)


def test_the_empty_string_plans_to_nothing() -> None:
    assert g.plan_runs("") == ()


def test_is_drawable_is_strict_where_plan_runs_is_forgiving() -> None:
    assert g.is_drawable(f"speed {LESS_EQ} 5 {TICK}")
    assert not g.is_drawable("speed \U0001f600")
    assert g.plan_runs("speed \U0001f600") == (g.GlyphRun("primary", "speed "),)
