"""The Pillow-backed :class:`~edutoon.domain.visual_layout.TextMeasurer` (Phase 11).

``domain.visual_layout`` is pure, so it asks this object how text wraps and how big it is. Wrapping
is by whole words only - a word wider than the line makes the answer "does not fit" rather than
being split, and more lines than allowed likewise - so a visual's labels are never cut, ellipsised
or broken mid-word. Text is measured through the same :class:`TextFace` the drawing uses, so a
symbol drawn from the fallback font, or a check mark, is measured as it will be drawn.

The width used for wrapping is deliberately 6% narrower than the width the layout asks about: a
tiny difference in font metrics between machines can then never tip a line over the edge of its
box, and a layout decision never flips on a sub-pixel.
"""

from __future__ import annotations

from edutoon.domain.visual_layout import TextBlock
from edutoon.providers.renderer.compositor import TextFace, _line_pitch

HEADROOM_PERCENT = 94
LINE_GAP_RATIO = 0.08


class PillowTextMeasurer:
    def measure(
        self, text: str, *, size: int, weight: int, max_width: int, max_lines: int
    ) -> TextBlock | None:
        face = TextFace(mono=False, weight=weight, size=size)
        limit = max_width * HEADROOM_PERCENT // 100
        lines: list[str] = []
        current = ""
        for word in text.split():
            if face.getlength(word) > limit:
                return None
            candidate = word if not current else f"{current} {word}"
            if face.getlength(candidate) <= limit:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        if not lines or len(lines) > max_lines:
            return None
        pitch = _line_pitch(face, LINE_GAP_RATIO)
        width = max(round(face.getlength(line)) for line in lines)
        return TextBlock(lines=tuple(lines), width=width, height=pitch * len(lines), pitch=pitch)
