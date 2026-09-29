"""Paints a laid-out procedural visual as compositor layers (Phase 11).

``domain.visual_layout`` decides *where* everything goes (integers, already measured and checked);
this module only draws what it is told: one small RGBA layer per box (its shape, text and number
disc together), one tight-cropped layer per connector, and one for the title. Nothing here measures,
positions, wraps or shrinks anything - a piece that did not fit would have made the layout raise
``LayoutOverflow`` before it got here.

Styling is by ``kind`` only (a step, a band, a heading, a point, the subject, the relation marker);
the palette is the compositor's own, so a visual sits comfortably beside the other templates.
Layers carry no slide - a node that slid while its connector stayed put would come apart for a
few frames - only a fade, so geometry is exact at every frame.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image

from edutoon.domain.visual_layout import LayoutBox, LayoutConnector, Units, VisualLayout
from edutoon.providers.renderer.compositor import (
    ACCENT,
    ACCENT_DEEP,
    ACCENT_LINE,
    ACCENT_SOFT,
    INK,
    INK_MUTED,
    WARM,
    WHITE,
    Layer,
)
from edutoon.providers.renderer.visual_primitives import (
    box_image,
    connector_image,
    marker_image,
    text_image,
)

Colour = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _Style:
    fill: Colour
    outline: Colour | None
    text: dict[str, Colour]
    radius: int  # reference pixels; a circle when it is at least half the box
    badge_fill: Colour = ACCENT


_TEXT_ON_LIGHT = {"label": INK, "detail": INK_MUTED, "point": INK}
_TEXT_ON_DARK = {"heading": WHITE, "subject": WHITE, "relation": WHITE, "label": WHITE}

_STYLES: dict[str, _Style] = {
    "node": _Style(WHITE, (214, 220, 240), _TEXT_ON_LIGHT, 24),
    "band": _Style(ACCENT_SOFT, ACCENT_LINE, _TEXT_ON_LIGHT, 20),
    "point": _Style(WHITE, (214, 220, 240), _TEXT_ON_LIGHT, 18),
    "heading": _Style(ACCENT, None, _TEXT_ON_DARK, 20),
    "subject": _Style(ACCENT_DEEP, None, _TEXT_ON_DARK, 28),
    "relation": _Style(WARM, None, _TEXT_ON_DARK, 1000),
}
_ACCENTED = _Style(
    ACCENT_SOFT,
    ACCENT,
    {"label": ACCENT_DEEP, "detail": INK_MUTED, "point": ACCENT_DEEP},
    24,
    badge_fill=ACCENT_DEEP,
)


def _text_colour(style: _Style, role: str) -> Colour:
    return style.text.get(role, INK)


def _box_picture(box: LayoutBox, units: Units) -> Image.Image:
    style = _ACCENTED if box.accent and box.kind in ("node", "band", "point") else _STYLES[box.kind]
    rect = box.rect
    radius = min(units.u(style.radius), min(rect.w, rect.h) // 2)
    picture = box_image(
        rect.w,
        rect.h,
        radius=radius,
        fill=style.fill,
        outline=style.outline,
        outline_width=max(2, units.u(3)) if style.outline else 0,
    )
    for text in box.texts:
        drawn = text_image(
            text.lines,
            size=text.size,
            weight=text.weight,
            pitch=text.pitch,
            colour=_text_colour(style, text.role),
            width=text.width,
            align=text.align,
        )
        picture.alpha_composite(drawn, (text.x - rect.x, text.y - rect.y))
    if box.badge is not None and box.number is not None:
        disc = marker_image(box.number, box.badge.w, fill=style.badge_fill)
        picture.alpha_composite(disc, (box.badge.x - rect.x, box.badge.y - rect.y))
    return picture


def _connector_layer(connector: LayoutConnector, units: Units, start_ms: float) -> Layer:
    if connector.kind == "arrow":
        picture, offset = connector_image(
            connector.points,
            stroke=max(3, units.u(6)),
            colour=ACCENT,
            head_length=units.u(30),
            head_half_width=units.u(16),
        )
    elif connector.kind == "leader":
        picture, offset = connector_image(
            connector.points,
            stroke=max(2, units.u(5)),
            colour=ACCENT_LINE,
            dot_radius=units.u(9),
        )
    else:
        picture, offset = connector_image(
            connector.points, stroke=max(2, units.u(5)), colour=ACCENT_LINE
        )
    return Layer.from_rgba(picture, offset[0], offset[1], start_ms, slide=False)


def paint_visual(layout: VisualLayout, *, units: Units, starts: Sequence[float]) -> list[Layer]:
    """The layers of ``layout``: its title (shown from the start), then every box and connector
    at the time of its step. ``starts[step]`` is when that step begins; there must be one for
    each of ``layout.steps``."""
    if len(starts) < layout.steps:
        raise ValueError("a start time is needed for every step of the visual.")
    layers: list[Layer] = []
    if layout.title is not None:
        title = layout.title
        picture = text_image(
            title.lines,
            size=title.size,
            weight=title.weight,
            pitch=title.pitch,
            colour=INK,
            width=title.width,
            align=title.align,
        )
        layers.append(Layer.from_rgba(picture, title.x, title.y, 0, slide=False))
    for box in layout.boxes:
        layers.append(
            Layer.from_rgba(
                _box_picture(box, units), box.rect.x, box.rect.y, starts[box.step], slide=False
            )
        )
    for connector in layout.connectors:
        layers.append(_connector_layer(connector, units, starts[connector.step]))
    return layers
