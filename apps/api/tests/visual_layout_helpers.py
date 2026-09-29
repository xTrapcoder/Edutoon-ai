"""Shared by the layout tests: a fake text measurer, standard regions, and an *independent*
checker of what a clean layout must satisfy (it does not call ``check_layout``, so it can catch
a bug in it).
"""

from __future__ import annotations

import math

from edutoon.domain import visual_layout as vl

# The safe areas the compositor gives a visual on a 1080-pixel short side.
REGIONS = {
    ("horizontal", "full"): vl.Rect(140, 96, 1640, 888),
    ("horizontal", "half"): vl.Rect(140, 96, 760, 888),
    ("vertical", "full"): vl.Rect(80, 150, 920, 1620),
    ("vertical", "half"): vl.Rect(80, 150, 920, 760),
}
FULL_SHORT_SIDE = 1080


def region_for(orientation: str, size: str = "full", short_side: int = 1080) -> vl.Rect:
    base = REGIONS[(orientation, size)]
    if short_side == 1080:
        return base
    scale = short_side / 1080
    return vl.Rect(*(round(v * scale) for v in (base.x, base.y, base.w, base.h)))


class FixedMeasurer:
    """Every character is ``width_percent`` % of the font size wide; a line is ``pitch_percent``
    % of it tall. Wraps greedily between words and answers ``None`` exactly as the real measurer
    does (a word too wide, or too many lines). ``max_size`` makes everything above it not fit."""

    def __init__(
        self, width_percent: int = 55, pitch_percent: int = 130, max_size: int | None = None
    ):
        self.width_percent = width_percent
        self.pitch_percent = pitch_percent
        self.max_size = max_size
        self.calls = 0

    def _width(self, text: str, size: int) -> int:
        return -(-len(text) * size * self.width_percent // 100)

    def measure(self, text, *, size, weight, max_width, max_lines):
        self.calls += 1
        if self.max_size is not None and size > self.max_size:
            return None
        lines: list[str] = []
        current = ""
        for word in text.split():
            if self._width(word, size) > max_width:
                return None
            candidate = word if not current else f"{current} {word}"
            if self._width(candidate, size) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        if not lines or len(lines) > max_lines:
            return None
        pitch = size * self.pitch_percent // 100
        return vl.TextBlock(
            lines=tuple(lines),
            width=max(self._width(line, size) for line in lines),
            height=pitch * len(lines),
            pitch=pitch,
        )


def _sample(a, b, step=2):
    length = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1)
    count = max(1, length // step)
    return [
        (a[0] + (b[0] - a[0]) * i // count, a[1] + (b[1] - a[1]) * i // count)
        for i in range(count + 1)
    ]


def assert_clean(layout: vl.VisualLayout, region: vl.Rect, short_side: int = 1080) -> None:
    """Every guarantee a layout makes, checked from first principles."""
    units = vl.Units(short_side)
    boxes = layout.boxes
    assert boxes, "a layout has at least one box"

    # 1. every box is inside the region and no two overlap
    for box in boxes:
        r = box.rect
        assert r.w > 0 and r.h > 0
        assert region.x <= r.x and region.y <= r.y, f"{box.kind} above/left of the region"
        assert r.x + r.w <= region.x + region.w, f"{box.kind} beyond the right edge"
        assert r.y + r.h <= region.y + region.h, f"{box.kind} beyond the bottom edge"
    for i, a in enumerate(boxes):
        for j, b in enumerate(boxes):
            if i < j:
                apart = (
                    a.rect.x + a.rect.w <= b.rect.x
                    or b.rect.x + b.rect.w <= a.rect.x
                    or a.rect.y + a.rect.h <= b.rect.y
                    or b.rect.y + b.rect.h <= a.rect.y
                )
                assert apart, f"boxes {i} and {j} overlap"

    # 2. text stays inside its box, and is never smaller than its role's minimum
    for box in boxes:
        for text in box.texts:
            assert text.lines, "empty text block"
            assert text.x >= box.rect.x and text.x + text.width <= box.rect.x + box.rect.w
            assert text.y >= box.rect.y
            assert text.y + text.pitch * len(text.lines) <= box.rect.y + box.rect.h
            smallest = vl.ROLE_SPECS[text.role][1]
            assert text.size >= units.u(smallest), f"{text.role} is below its minimum size"
    if layout.title is not None:
        t = layout.title
        assert region.x <= t.x and t.x + t.width <= region.x + region.w
        assert t.size >= units.u(vl.ROLE_SPECS["title"][1])
        assert all(b.rect.y >= t.y + t.pitch * len(t.lines) for b in boxes), "a box under the title"

    # 3. every connector: inside the region, both ends on a box edge, never through a box
    for connector in layout.connectors:
        points = connector.points
        assert len(points) >= 2 and points[0] != points[-1] or len(points) > 2
        for x, y in points:
            assert region.x <= x <= region.x + region.w and region.y <= y <= region.y + region.h
        for end in (points[0], points[-1]):
            assert any(_on_edge(end, b.rect) for b in boxes), f"detached end {end}"
        for a, b in zip(points, points[1:], strict=False):
            for x, y in _sample(a, b):
                for box in boxes:
                    r = box.rect
                    assert not (r.x < x < r.x + r.w and r.y < y < r.y + r.h), "through a box"
        if connector.kind == "arrow":
            length = sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
            assert length >= units.u(vl.MIN_ARROW) * 0.95, "an arrow too short to read"

    # 4. all integers, and the steps are consistent
    for box in boxes:
        assert all(isinstance(v, int) for v in (box.rect.x, box.rect.y, box.rect.w, box.rect.h))
    for connector in layout.connectors:
        assert all(isinstance(c, int) for p in connector.points for c in p)
    used = [b.step for b in boxes] + [c.step for c in layout.connectors]
    assert layout.steps == max(used) + 1
    assert min(used) == 0


def _on_edge(point, r) -> bool:
    x, y = point
    inside = r.x <= x <= r.x + r.w and r.y <= y <= r.y + r.h
    return inside and (x in (r.x, r.x + r.w) or y in (r.y, r.y + r.h))


def all_text(layout: vl.VisualLayout) -> list[str]:
    """Every piece of text of the layout, each joined back into one string."""
    out = [" ".join(t.lines) for box in layout.boxes for t in box.texts]
    if layout.title is not None:
        out.append(" ".join(layout.title.lines))
    return out
