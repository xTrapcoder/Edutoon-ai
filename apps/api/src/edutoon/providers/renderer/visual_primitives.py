"""Drawing primitives for procedural visuals (Phase 11): boxes, number badges, connectors.

Every function returns a small, *tight-cropped* RGBA picture (and, where it matters, the offset to
paste it at) - never a full-canvas layer. That is a measured requirement, not a preference: a
full-canvas connector layer costs ~180 ms whatever it holds, a tight-cropped arrow ~8 ms.

**Deterministic by construction.** All geometry here is integer arithmetic on integer pixel
coordinates: a connector's direction is normalised with :func:`math.isqrt` into a fixed-point
vector (1024 = 1.0), the arrowhead's corners are computed from that vector with integer division
(round half away from zero), and shapes are rasterised by Pillow at 2x and reduced with LANCZOS -
the same scheme as the existing ``panel``. No ``sin``/``cos``/``sqrt``, no float geometry, no random
numbers, no clock.

The layout that decides *where* things go is ``domain.visual_layout``; this module only draws what
it is told to, and never decides a position.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isqrt

from PIL import Image, ImageDraw

from edutoon.providers.renderer.compositor import (
    ACCENT,
    SUPERSAMPLE,
    WHITE,
    FittedText,
    TextFace,
    fit_text,
    panel,
    text_layer,
)

Point = tuple[int, int]
Colour = tuple[int, int, int]

_FIXED = 1024  # fixed-point 1.0 for direction vectors


def _div_round(numerator: int, denominator: int) -> int:
    """``numerator / denominator`` rounded half away from zero, in integers."""
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    if numerator >= 0:
        return (2 * numerator + denominator) // (2 * denominator)
    return -((2 * -numerator + denominator) // (2 * denominator))


def direction(start: Point, end: Point) -> tuple[int, int]:
    """The unit vector from ``start`` to ``end`` in fixed point (1024 = 1.0). A zero-length
    segment has no direction and is a caller error."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = isqrt(dx * dx + dy * dy)
    if length == 0:
        raise ValueError("a connector segment must have a length.")
    return _div_round(dx * _FIXED, length), _div_round(dy * _FIXED, length)


def arrowhead(tip: Point, toward: tuple[int, int], length: int, half_width: int) -> list[Point]:
    """The three corners of an arrowhead whose point is at ``tip`` and that points along the
    fixed-point unit vector ``toward``: the tip, then the two corners of its base, all integers."""
    ux, uy = toward
    base = (tip[0] - _div_round(ux * length, _FIXED), tip[1] - _div_round(uy * length, _FIXED))
    wing = (_div_round(-uy * half_width, _FIXED), _div_round(ux * half_width, _FIXED))
    return [tip, (base[0] + wing[0], base[1] + wing[1]), (base[0] - wing[0], base[1] - wing[1])]


def connector_image(
    points: Sequence[Point],
    *,
    stroke: int,
    colour: Colour,
    head_length: int = 0,
    head_half_width: int = 0,
    dot_radius: int = 0,
) -> tuple[Image.Image, Point]:
    """A polyline through ``points`` as a tight RGBA picture, and where to paste it.

    ``head_length`` > 0 puts an arrowhead at the last point, pointing along the last segment (the
    shaft stops inside the head, so no line pokes through the tip). ``dot_radius`` > 0 puts a
    filled dot on the last point (a leader line's end). The first point is always capped with a
    round end. The picture is exactly the bounding box of everything drawn, plus a 2-pixel margin.
    """
    if len(points) < 2:
        raise ValueError("a connector needs at least two points.")
    path = [(int(x), int(y)) for x, y in points]
    head: list[Point] | None = None
    if head_length > 0:
        toward = direction(path[-2], path[-1])
        head = arrowhead(path[-1], toward, head_length, head_half_width)
        # The shaft ends half a head-length short of the tip, inside the head's body.
        keep = max(0, _segment_length(path[-2], path[-1]) - head_length // 2)
        path[-1] = (
            path[-2][0] + _div_round(toward[0] * keep, _FIXED),
            path[-2][1] + _div_round(toward[1] * keep, _FIXED),
        )

    drawn = list(path) + (head or [])
    if dot_radius:
        drawn.append(points[-1])
    margin = max(stroke, head_half_width, dot_radius) + 2
    left = min(x for x, _ in drawn) - margin
    top = min(y for _, y in drawn) - margin
    width = max(x for x, _ in drawn) + margin + 1 - left
    height = max(y for _, y in drawn) + margin + 1 - top

    sample = SUPERSAMPLE
    big = Image.new("RGBA", (width * sample, height * sample), (*colour, 0))
    pen = ImageDraw.Draw(big)
    ink = (*colour, 255)

    def big_point(point: Point) -> Point:
        return ((point[0] - left) * sample, (point[1] - top) * sample)

    line = [big_point(p) for p in path]
    pen.line(line, fill=ink, width=stroke * sample, joint="curve")
    radius = stroke * sample // 2
    caps = [line[0]] + ([line[-1]] if head is None and not dot_radius else [])
    for cx, cy in caps:
        pen.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=ink)
    if head is not None:
        pen.polygon([big_point(p) for p in head], fill=ink)
    if dot_radius:
        cx, cy = big_point(points[-1])
        r = dot_radius * sample
        pen.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ink)
    return big.resize((width, height), Image.Resampling.LANCZOS), (left, top)


def _segment_length(a: Point, b: Point) -> int:
    return isqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2)


def box_image(
    width: int,
    height: int,
    *,
    radius: int,
    fill: Colour,
    outline: Colour | None = None,
    outline_width: int = 0,
) -> Image.Image:
    """A rounded box (a thin, named wrapper on the compositor's anti-aliased ``panel``)."""
    return panel(width, height, radius, fill, outline=outline, outline_width=outline_width)


def marker_image(
    number: int, diameter: int, *, fill: Colour = ACCENT, ink: Colour = WHITE, weight: int = 700
) -> Image.Image:
    """A filled circle carrying a step number. The digits are renderer-generated ordinals - they
    label position, they are not content, and they are never taken from a specification."""
    big = diameter * SUPERSAMPLE
    disc = Image.new("RGBA", (big, big), (*fill, 0))
    ImageDraw.Draw(disc).ellipse((0, 0, big - 1, big - 1), fill=(*fill, 255))
    disc = disc.resize((diameter, diameter), Image.Resampling.LANCZOS)
    label = fit_text(
        str(number),
        max_width=diameter,
        max_height=diameter,
        max_size=max(8, round(diameter * 0.62)),
        min_size=8,
        weight=weight,
    )
    layer = text_layer(label, ink, width=diameter, align="center")
    disc.alpha_composite(layer, (0, max(0, (diameter - label.height) // 2)))
    return disc


def text_image(
    lines: Sequence[str],
    *,
    size: int,
    weight: int,
    pitch: int,
    colour: Colour,
    width: int,
    align: str = "left",
    mono: bool = False,
) -> Image.Image:
    """Already-wrapped ``lines`` as a picture ``width`` wide. The layout measured and wrapped
    these lines with the same face, so nothing here re-wraps, shrinks or cuts a word."""
    face = TextFace(mono=mono, weight=weight, size=size)
    fitted = FittedText(face=face, lines=list(lines), pitch=pitch, size=size)
    return text_layer(fitted, colour, width=width, align=align)


def divider_image(width: int, thickness: int, colour: Colour) -> Image.Image:
    """A short rounded bar (used to underline a title)."""
    return panel(width, thickness, max(1, thickness // 2), colour)
