"""Which characters the bundled fonts can draw, and how a line of text is split into runs that can
all be drawn - pure domain logic (rule 1: no I/O, no font files, no imports of repositories /
providers / services).

**The defect this closes.** The bundled Noto Sans has no glyphs for ``→ ← ≤ ≥ ≈ ≠ √ ✓`` and many
other symbols, so any of them in a scene's text used to render as the font's missing-glyph box.
Noto Sans Mono (also bundled, same licence) does have the arrows, comparison and root signs and
the geometric shapes. This module therefore plans every line as a sequence of :class:`GlyphRun`
objects:

- ``primary`` - drawn with the template's own font (Sans for prose, Mono for code and formulae);
- ``fallback`` - a character the primary font lacks but the *other* bundled font has (a Mono
  arrow inside a Sans sentence), drawn at the same size and baseline;
- ``tick`` / ``cross`` - the check and cross marks (``✓ ✔`` and ``✗ ✘ ✕ ✖``), which *neither*
  font has. They are drawn as small shapes by the compositor, so no icon font or asset is added;
- anything else that neither font can draw (emoji, control and format characters, private use) is
  **dropped**, never drawn as a box. A few close substitutes are applied first
  (:data:`SUBSTITUTES`, e.g. ``⇄`` -> ``↔``).

The coverage tables below are the fonts' own cmap ranges (``fontTools`` was used once to
generate them, so nothing here needs a font parser at run time); ``tests/test_text_glyphs.py``
recomputes them from the font files and fails if a font is replaced without regenerating them.
"""

from __future__ import annotations

import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal

# --- generated from the bundled fonts' cmaps (codepoints >= U+0020) --------------------------

SANS_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x0020, 0x007E),
    (0x00A0, 0x0377),
    (0x037A, 0x037F),
    (0x0384, 0x038A),
    (0x038C, 0x038C),
    (0x038E, 0x03A1),
    (0x03A3, 0x03E1),
    (0x03F0, 0x052F),
    (0x0900, 0x097F),
    (0x10FB, 0x10FB),
    (0x1AB0, 0x1AC0),
    (0x1AC5, 0x1AC5),
    (0x1AC7, 0x1ACE),
    (0x1C80, 0x1C88),
    (0x1D00, 0x1DF9),
    (0x1DFB, 0x1F15),
    (0x1F18, 0x1F1D),
    (0x1F20, 0x1F45),
    (0x1F48, 0x1F4D),
    (0x1F50, 0x1F57),
    (0x1F59, 0x1F59),
    (0x1F5B, 0x1F5B),
    (0x1F5D, 0x1F5D),
    (0x1F5F, 0x1F7D),
    (0x1F80, 0x1FB4),
    (0x1FB6, 0x1FC4),
    (0x1FC6, 0x1FD3),
    (0x1FD6, 0x1FDB),
    (0x1FDD, 0x1FEF),
    (0x1FF2, 0x1FF4),
    (0x1FF6, 0x1FFE),
    (0x2000, 0x2064),
    (0x2066, 0x2071),
    (0x2074, 0x208E),
    (0x2090, 0x209C),
    (0x20A0, 0x20C0),
    (0x20F0, 0x20F0),
    (0x2100, 0x215F),
    (0x2183, 0x2184),
    (0x2189, 0x2189),
    (0x2212, 0x2212),
    (0x25CC, 0x25CC),
    (0x2C60, 0x2C7F),
    (0x2DE0, 0x2E5D),
    (0xA640, 0xA69F),
    (0xA700, 0xA7CA),
    (0xA7D0, 0xA7D1),
    (0xA7D3, 0xA7D3),
    (0xA7D5, 0xA7D9),
    (0xA7F2, 0xA7FF),
    (0xA8FF, 0xA8FF),
    (0xA92E, 0xA92E),
    (0xAB30, 0xAB6B),
    (0xFB00, 0xFB06),
    (0xFE00, 0xFE00),
    (0xFE20, 0xFE2F),
    (0xFEFF, 0xFEFF),
    (0xFFFC, 0xFFFD),
    (0x10780, 0x10785),
    (0x10787, 0x107B0),
    (0x107B2, 0x107BA),
    (0x1DF00, 0x1DF1E),
)

MONO_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x0020, 0x007E),
    (0x00A0, 0x00AC),
    (0x00AE, 0x0377),
    (0x037A, 0x037F),
    (0x0384, 0x038A),
    (0x038C, 0x038C),
    (0x038E, 0x03A1),
    (0x03A3, 0x03E1),
    (0x03F0, 0x052F),
    (0x10FB, 0x10FB),
    (0x1AB0, 0x1AC0),
    (0x1AC5, 0x1AC5),
    (0x1AC7, 0x1ACE),
    (0x1C80, 0x1C88),
    (0x1D00, 0x1DF9),
    (0x1DFB, 0x1F15),
    (0x1F18, 0x1F1D),
    (0x1F20, 0x1F45),
    (0x1F48, 0x1F4D),
    (0x1F50, 0x1F57),
    (0x1F59, 0x1F59),
    (0x1F5B, 0x1F5B),
    (0x1F5D, 0x1F5D),
    (0x1F5F, 0x1F7D),
    (0x1F80, 0x1FB4),
    (0x1FB6, 0x1FC4),
    (0x1FC6, 0x1FD3),
    (0x1FD6, 0x1FDB),
    (0x1FDD, 0x1FEF),
    (0x1FF2, 0x1FF4),
    (0x1FF6, 0x1FFE),
    (0x2000, 0x2064),
    (0x2066, 0x2071),
    (0x2074, 0x208E),
    (0x2090, 0x209C),
    (0x20A0, 0x20C0),
    (0x20F0, 0x20F0),
    (0x2100, 0x215F),
    (0x2183, 0x2184),
    (0x2189, 0x2189),
    (0x2190, 0x2195),
    (0x219C, 0x219E),
    (0x21A0, 0x21A0),
    (0x21A2, 0x21A4),
    (0x21A6, 0x21A6),
    (0x21D0, 0x21D4),
    (0x21DA, 0x21DB),
    (0x21E6, 0x21E6),
    (0x21E8, 0x21E8),
    (0x2200, 0x220E),
    (0x2210, 0x2210),
    (0x2212, 0x2212),
    (0x2218, 0x221A),
    (0x221E, 0x221E),
    (0x2220, 0x2220),
    (0x2223, 0x2223),
    (0x2227, 0x222A),
    (0x2234, 0x2238),
    (0x223C, 0x223D),
    (0x2241, 0x2241),
    (0x2243, 0x2243),
    (0x2245, 0x2245),
    (0x2247, 0x224C),
    (0x2254, 0x2255),
    (0x2257, 0x2257),
    (0x225F, 0x2262),
    (0x2264, 0x2265),
    (0x226C, 0x226C),
    (0x226E, 0x2275),
    (0x227A, 0x227B),
    (0x2282, 0x2289),
    (0x228E, 0x228E),
    (0x2291, 0x229C),
    (0x22A2, 0x22A5),
    (0x22B4, 0x22B5),
    (0x22B8, 0x22B8),
    (0x22C2, 0x22C4),
    (0x22C6, 0x22C6),
    (0x22C8, 0x22CA),
    (0x22CD, 0x22CE),
    (0x22D0, 0x22D1),
    (0x22E2, 0x22E3),
    (0x2308, 0x230B),
    (0x2310, 0x2310),
    (0x2319, 0x2319),
    (0x2320, 0x2321),
    (0x2336, 0x237A),
    (0x2395, 0x2395),
    (0x239B, 0x23AE),
    (0x23B0, 0x23BD),
    (0x23DC, 0x23E1),
    (0x2474, 0x2475),
    (0x2500, 0x25FF),
    (0x266D, 0x266F),
    (0x2736, 0x2736),
    (0x2758, 0x275A),
    (0x27D5, 0x27D7),
    (0x27DC, 0x27DC),
    (0x27E6, 0x27EB),
    (0x27F5, 0x27F6),
    (0x2987, 0x2988),
    (0x29A3, 0x29A3),
    (0x29B8, 0x29B8),
    (0x2A00, 0x2A00),
    (0x2A05, 0x2A06),
    (0x2C60, 0x2C7F),
    (0x2DE0, 0x2E5D),
    (0xA640, 0xA69F),
    (0xA700, 0xA7CA),
    (0xA7D0, 0xA7D1),
    (0xA7D3, 0xA7D3),
    (0xA7D5, 0xA7D9),
    (0xA7F2, 0xA7FF),
    (0xA92E, 0xA92E),
    (0xAB30, 0xAB6B),
    (0xFE00, 0xFE00),
    (0xFE20, 0xFE2F),
    (0xFEFF, 0xFEFF),
    (0xFF5B, 0xFF5B),
    (0xFF5D, 0xFF5D),
    (0xFFFC, 0xFFFD),
    (0x10780, 0x10785),
    (0x10787, 0x107B0),
    (0x107B2, 0x107BA),
    (0x1DF00, 0x1DF1E),
    (0x1F67C, 0x1F67F),
)

TICK_CHARS: Final = "✓✔"  # check mark, heavy check mark
CROSS_CHARS: Final = "✗✘✕✖"  # ballot x, heavy ballot x, multiplication x
# The nearest character a font *does* have, applied only where neither font (nor a drawn mark)
# covers the original. The target must itself be drawable; the tests assert that it is.
SUBSTITUTES: Final[dict[str, str]] = {
    "⇄": "↔",  # rightwards arrow over leftwards arrow -> left right arrow
    "∑": "Σ",  # n-ary summation -> Greek capital sigma
    "­": "",  # soft hyphen: never drawn
}

RunKind = Literal["primary", "fallback", "tick", "cross"]
Primary = Literal["sans", "mono"]


@dataclass(frozen=True, slots=True)
class GlyphRun:
    """A stretch of one line that is drawn one way. ``text`` is empty for nothing; for a
    ``tick`` or ``cross`` it is the mark's own character(s)."""

    kind: RunKind
    text: str


def _starts_and_ends(
    ranges: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(lo for lo, _ in ranges), tuple(hi for _, hi in ranges)


_SANS = _starts_and_ends(SANS_RANGES)
_MONO = _starts_and_ends(MONO_RANGES)


def _covered(codepoint: int, table: tuple[tuple[int, ...], tuple[int, ...]]) -> bool:
    starts, ends = table
    index = bisect_right(starts, codepoint) - 1
    return index >= 0 and codepoint <= ends[index]


def in_sans(character: str) -> bool:
    """Whether the bundled Noto Sans can draw ``character``."""
    return _covered(ord(character), _SANS)


def in_mono(character: str) -> bool:
    """Whether the bundled Noto Sans Mono can draw ``character``."""
    return _covered(ord(character), _MONO)


def _is_dropped(character: str) -> bool:
    """Characters that must never be drawn: controls, format characters (zero-width and bidi
    controls included), surrogates, private use, unassigned code points and variation
    selectors. A tab is treated as a space before this is asked."""
    codepoint = ord(character)
    if 0xFE00 <= codepoint <= 0xFE0F:
        return True
    return unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}


def glyph_kind(character: str, *, primary: Primary = "sans") -> RunKind | None:
    """How ``character`` is drawn when the template's font is ``primary``, or ``None`` if it
    cannot be drawn at all (it would be dropped, or replaced by a substitute). Control, format
    (zero-width, bidi override, soft hyphen), private-use and similar characters are never
    drawable even where a font happens to carry an invisible glyph for them."""
    if _is_dropped(character):
        return None
    if character in TICK_CHARS:
        return "tick"
    if character in CROSS_CHARS:
        return "cross"
    own, other = (in_sans, in_mono) if primary == "sans" else (in_mono, in_sans)
    if own(character):
        return "primary"
    if other(character):
        return "fallback"
    return None


def is_drawable(text: str) -> bool:
    """Whether every character of ``text`` can be drawn as written (prose font). Stricter than
    :func:`plan_runs`, which drops what it cannot draw: a validator uses this to *reject* text
    rather than silently lose part of it."""
    return all(glyph_kind(character) is not None for character in text)


@lru_cache(maxsize=4096)
def plan_runs(text: str, *, primary: Primary = "sans") -> tuple[GlyphRun, ...]:
    """Split ``text`` (one line, no newline) into drawable runs; see the module docstring.

    Adjacent characters of the same kind share a run, so a line with nothing unusual in it is a
    single ``primary`` run - the compositor then draws it exactly as it always did.
    """
    runs: list[GlyphRun] = []

    def add(kind: RunKind, chunk: str) -> None:
        if not chunk:
            return
        if runs and runs[-1].kind == kind and kind in ("primary", "fallback"):
            runs[-1] = GlyphRun(kind, runs[-1].text + chunk)
        else:
            runs.append(GlyphRun(kind, chunk))

    for original in text:
        character = " " if original == "\t" else original
        kind = glyph_kind(character, primary=primary)
        if kind is None:
            character = SUBSTITUTES.get(character, character)
            if character == "":
                continue
            kind = glyph_kind(character, primary=primary)
        if kind is None:
            continue
        add(kind, character)
    return tuple(runs)
