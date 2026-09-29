"""The Pillow scene compositor: draws one Phase 9 scene, frame by frame.

A scene is turned into a small list of :class:`Layer` objects (a panel, a title, a bullet, a
diagram box ...), each a pre-drawn RGBA picture with its own start time. A frame is then just
the background with each layer pasted at the opacity / offset / reveal width the animation
functions in ``domain.render_animation`` give for that frame's local time. Nothing is drawn
per frame except a handful of pastes, so a whole scene costs roughly as much as its distinct
frames, and consecutive frames whose animation state is identical are the very same bytes
(:meth:`SceneCompositor.state_key`) - a still scene is drawn once.

**All six Phase 9 templates render from text alone.** ``title_card``, ``bullet_points`` and
``formula_or_code`` never had an illustration. The other three - ``labelled_diagram``,
``split_text_illustration``, ``full_bleed_illustration`` - draw a *procedural visual* (Phase 11:
process flow, cycle, hierarchy, comparison, labelled components - see ``domain.visual``) when the
scene has a valid one that fits, and otherwise a deliberate text fallback (numbered parts, the
sentence at full width, or a statement card). None of them ever draws an empty panel or implies a
picture exists; no image is ever generated.

Layout follows ``domain.storyboard_template.layout_for`` (the arrangement tag chosen for the
scene's orientation), and every size is derived from the canvas so the same code serves
1920x1080, 1080x1920 and the half-size preview.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from edutoon.domain import render_animation as anim
from edutoon.domain.storyboard_template import layout_for
from edutoon.domain.text_glyphs import Primary, plan_runs
from edutoon.domain.visual_layout import Rect, Units, VisualLayout

_FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_SANS = _FONT_DIR / "NotoSans-VF.ttf"
_MONO = _FONT_DIR / "NotoSansMono-VF.ttf"

# --- palette --------------------------------------------------------------------------

INK = (27, 37, 64)
INK_MUTED = (92, 103, 132)
PAPER_TOP = (251, 250, 247)
PAPER_BOTTOM = (240, 238, 231)
ACCENT = (59, 91, 219)
ACCENT_DEEP = (38, 58, 140)
ACCENT_SOFT = (232, 237, 251)
ACCENT_LINE = (127, 149, 227)
WARM = (240, 140, 0)
WHITE = (255, 255, 255)
CODE_BG = (20, 27, 52)
CODE_INK = (226, 232, 255)
CODE_MUTED = (146, 160, 205)

# --- fonts (FreeType faces are not thread-safe, so each render thread keeps its own) ----

_thread_fonts = threading.local()


def font(*, mono: bool, weight: int, size: int) -> ImageFont.FreeTypeFont:
    cache: dict[tuple[bool, int, int], ImageFont.FreeTypeFont]
    cache = _thread_fonts.__dict__.setdefault("cache", {})
    key = (mono, weight, size)
    face = cache.get(key)
    if face is None:
        face = ImageFont.truetype(str(_MONO if mono else _SANS), size)
        face.set_variation_by_axes([weight, 100])
        cache[key] = face
    return face


class Measurable(Protocol):
    def getlength(self, text: str) -> float: ...


FALLBACK_SCALE = 1.18


class TextFace:
    """One font at one size and weight that can draw every character the scene may contain.

    Noto Sans has no glyph for ``-> <= >= ~= ✓`` and many other symbols, which used to come out as
    the font's missing-glyph box. ``domain.text_glyphs`` plans each line into runs: text the face's
    own font has is drawn as it always was; a symbol only the *other* bundled font has (a Mono
    arrow inside a Sans sentence) is drawn from that font at the same size and baseline; check and
    cross marks are drawn as small shapes; what nothing can draw is dropped. A line with nothing
    unusual in it is one run and is drawn by the very same call as before, pixel for pixel.
    """

    __slots__ = ("_kind", "fallback", "primary", "size")

    def __init__(self, *, mono: bool, weight: int, size: int) -> None:
        self.primary = font(mono=mono, weight=weight, size=size)
        # Mono's arrows and operators are drawn a little small beside Sans letters; a modest
        # boost (a fixed ratio, so layout stays deterministic) makes them read as intended.
        self.fallback = font(mono=not mono, weight=weight, size=round(size * FALLBACK_SCALE))
        self._kind: Primary = "mono" if mono else "sans"
        self.size = size

    def getmetrics(self) -> tuple[int, int]:
        return self.primary.getmetrics()

    def _mark_advance(self) -> int:
        return max(1, round(self.size * 0.9))

    def getlength(self, text: str) -> float:
        total = 0.0
        for run in plan_runs(text, primary=self._kind):
            if run.kind == "primary":
                total += self.primary.getlength(run.text)
            elif run.kind == "fallback":
                total += self.fallback.getlength(run.text)
            else:
                total += self._mark_advance() * len(run.text)
        return total

    def draw_line(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        x: float,
        y: float,
        text: str,
        fill: tuple[int, int, int],
    ) -> None:
        """Draw ``text`` with its top at ``y`` (the font's ascender line, as ``ImageDraw.text``
        does by default) onto ``image`` (RGBA), which ``draw`` draws on."""
        runs = plan_runs(text, primary=self._kind)
        if len(runs) == 1 and runs[0].kind == "primary" and runs[0].text == text:
            draw.text((x, y), text, font=self.primary, fill=(*fill, 255))
            return
        baseline = y + self.primary.getmetrics()[0]
        cursor = x
        for run in runs:
            if run.kind in ("primary", "fallback"):
                face = self.primary if run.kind == "primary" else self.fallback
                draw.text((cursor, baseline), run.text, font=face, fill=(*fill, 255), anchor="ls")
                cursor += face.getlength(run.text)
            else:
                for _ in run.text:
                    self._draw_mark(image, run.kind, round(cursor), round(baseline), fill)
                    cursor += self._mark_advance()

    def _draw_mark(
        self,
        image: Image.Image,
        kind: str,
        x: int,
        baseline: int,
        fill: tuple[int, int, int],
    ) -> None:
        """A check or cross mark, drawn from shapes (anti-aliased at 2x) about as tall as a
        capital letter and standing on the baseline."""
        width = max(4, round(self.size * 0.78))
        height = max(4, round(self.size * 0.72))
        sample = SUPERSAMPLE
        big_w, big_h = width * sample, height * sample
        stroke = max(2, round(self.size * 0.11)) * sample
        mark = Image.new("RGBA", (big_w, big_h), (*fill, 0))
        pen = ImageDraw.Draw(mark)
        if kind == "tick":
            points = [(0.06, 0.56), (0.36, 0.90), (0.94, 0.10)]
            strokes = [points]
        else:
            strokes = [[(0.12, 0.12), (0.88, 0.88)], [(0.88, 0.12), (0.12, 0.88)]]
        for path in strokes:
            scaled = [(round(px * (big_w - 1)), round(py * (big_h - 1))) for px, py in path]
            pen.line(scaled, fill=(*fill, 255), width=stroke, joint="curve")
            for cx, cy in (scaled[0], scaled[-1]):
                pen.ellipse(
                    (cx - stroke // 2, cy - stroke // 2, cx + stroke // 2, cy + stroke // 2),
                    fill=(*fill, 255),
                )
        mark = mark.resize((width, height), Image.Resampling.LANCZOS)
        left = max(0, x + round(self.size * 0.05))
        top = max(0, baseline - height)
        image.alpha_composite(mark, (left, top))


# --- primitives ---------------------------------------------------------------------


@dataclass(slots=True)
class Layer:
    """One pre-drawn element. ``wipe`` reveals it progressively: left to right over the scene's
    reveal window by default (code, formulae), or - with ``wipe_ms`` - in ``wipe_dir`` (``right``,
    ``left``, ``down`` or ``up``) over that many milliseconds from ``start_ms`` (a connector being
    drawn from its tail to its tip). ``fade=False`` makes it follow only the whole-scene
    envelope."""

    rgb: Image.Image
    alpha: Image.Image
    x: int
    y: int
    start_ms: float
    fade: bool = True
    slide: bool = True
    wipe: bool = False
    wipe_dir: str = "right"
    wipe_ms: float | None = None

    @staticmethod
    def from_rgba(image: Image.Image, x: int, y: int, start_ms: float, **kwargs: Any) -> Layer:
        return Layer(image.convert("RGB"), image.getchannel("A"), x, y, start_ms, **kwargs)


_background_cache: dict[tuple[int, int], Image.Image] = {}
_background_lock = threading.Lock()


def background(width: int, height: int) -> Image.Image:
    """The shared paper-coloured gradient every scene sits on (shared so that the dip to
    background between two scenes is the same colour on both sides of the cut)."""
    with _background_lock:
        cached = _background_cache.get((width, height))
        if cached is None:
            top = np.array(PAPER_TOP, dtype=np.float32)
            bottom = np.array(PAPER_BOTTOM, dtype=np.float32)
            ramp = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
            column = top + (bottom - top) * ramp
            array = np.broadcast_to(column, (height, width, 3)).round().astype(np.uint8)
            cached = Image.fromarray(np.ascontiguousarray(array), "RGB")
            _background_cache[(width, height)] = cached
        return cached


def _line_pitch(face: TextFace, gap_ratio: float) -> int:
    ascent, descent = face.getmetrics()
    return round((ascent + descent) * (1.0 + gap_ratio))


def wrap_text(text: str, face: Measurable, max_width: int) -> list[str]:
    """Greedy word wrap honouring explicit newlines and each line's leading indentation; a
    word wider than the line is split by character rather than allowed to overflow."""
    lines: list[str] = []
    for paragraph in text.split(chr(10)):
        stripped = paragraph.lstrip(" ")
        indent = paragraph[: len(paragraph) - len(stripped)]
        current = ""
        for word in stripped.split(" "):
            candidate = word if not current else f"{current} {word}"
            if face.getlength(indent + candidate) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(indent + current)
                current = ""
            while face.getlength(indent + word) > max_width and len(word) > 1:
                cut = len(word) - 1
                while cut > 1 and face.getlength(indent + word[:cut]) > max_width:
                    cut -= 1
                lines.append(indent + word[:cut])
                word = word[cut:]
            current = word
        lines.append(indent + current)
    return lines


@dataclass(slots=True)
class FittedText:
    face: TextFace
    lines: list[str]
    pitch: int
    size: int

    @property
    def height(self) -> int:
        return self.pitch * len(self.lines)

    @property
    def width(self) -> int:
        return max((round(self.face.getlength(line)) for line in self.lines), default=0)


def fit_text(
    text: str,
    *,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int,
    weight: int = 400,
    mono: bool = False,
    gap_ratio: float = 0.0,
    max_lines: int | None = None,
) -> FittedText:
    """The largest size in ``[min_size, max_size]`` at which ``text`` wraps to fit the box. At
    the minimum size text that still does not fit is cut with an ellipsis rather than being
    allowed to run off the frame."""
    size = max_size
    while True:
        face = TextFace(mono=mono, weight=weight, size=size)
        lines = wrap_text(text, face, max_width)
        pitch = _line_pitch(face, gap_ratio)
        fits = pitch * len(lines) <= max_height and (max_lines is None or len(lines) <= max_lines)
        if fits or size <= min_size:
            break
        size = max(min_size, size - 2)
    limit = max(1, max_height // pitch)
    if max_lines is not None:
        limit = min(limit, max_lines)
    if len(lines) > limit:
        lines = lines[:limit]
        last = lines[-1]
        while last and face.getlength(last + "…") > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return FittedText(face=face, lines=lines, pitch=pitch, size=size)


def text_layer(
    fitted: FittedText,
    fill: tuple[int, int, int],
    *,
    width: int | None = None,
    align: str = "left",
) -> Image.Image:
    """The text as an RGBA picture whose colour is already ``fill`` everywhere (so
    anti-aliased edges blend with the right colour, not black)."""
    block_width = width if width is not None else max(fitted.width, 1)
    image = Image.new("RGBA", (block_width, max(fitted.height, 1)), (*fill, 0))
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(fitted.lines):
        line_width = fitted.face.getlength(line)
        if align == "center":
            x = (block_width - line_width) / 2
        elif align == "right":
            x = block_width - line_width
        else:
            x = 0
        fitted.face.draw_line(image, draw, x, index * fitted.pitch, line, fill)
    return image


SUPERSAMPLE = 2


def panel(
    width: int,
    height: int,
    radius: int,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
    *,
    outline: tuple[int, int, int] | None = None,
    outline_width: int = 0,
    dashed: bool = False,
    gradient_to: tuple[int, int, int] | None = None,
) -> Image.Image:
    """An anti-aliased rounded rectangle (drawn at 2x and reduced), optionally with a solid or
    dashed outline, optionally filled with a top-to-bottom gradient."""
    big_w, big_h, big_r = width * SUPERSAMPLE, height * SUPERSAMPLE, radius * SUPERSAMPLE
    base_colour = fill[:3] if len(fill) >= 3 else (0, 0, 0)
    mask = Image.new("L", (big_w, big_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big_w - 1, big_h - 1), big_r, fill=255)
    if gradient_to is not None:
        ramp = np.linspace(0.0, 1.0, big_h, dtype=np.float32)[:, None, None]
        top = np.array(base_colour, dtype=np.float32)
        bottom = np.array(gradient_to, dtype=np.float32)
        array = np.broadcast_to(top + (bottom - top) * ramp, (big_h, big_w, 3))
        colour = Image.fromarray(np.ascontiguousarray(array.round().astype(np.uint8)), "RGB")
    else:
        colour = Image.new("RGB", (big_w, big_h), base_colour)
    alpha_scale = fill[3] / 255 if len(fill) == 4 else 1.0
    if alpha_scale < 1.0:
        mask = mask.point(lambda v: round(v * alpha_scale))
    big = colour.convert("RGBA")
    big.putalpha(mask)
    if outline is not None and outline_width > 0:
        stroke = outline_width * SUPERSAMPLE
        draw = ImageDraw.Draw(big)
        if not dashed:
            draw.rounded_rectangle(
                (0, 0, big_w - 1, big_h - 1), big_r, outline=(*outline, 255), width=stroke
            )
        else:
            _dashed_outline(draw, big_w, big_h, big_r, outline, stroke)
    return big.resize((width, height), Image.Resampling.LANCZOS)


def _dashed_outline(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    radius: int,
    colour: tuple[int, int, int],
    stroke: int,
) -> None:
    dash, gap = stroke * 4, stroke * 3
    fill = (*colour, 255)
    half = stroke // 2

    def run(x0: int, y0: int, x1: int, y1: int) -> None:
        horizontal = y0 == y1
        start, end = (x0, x1) if horizontal else (y0, y1)
        position = start
        while position < end:
            stop = min(position + dash, end)
            if horizontal:
                draw.line((position, y0, stop, y0), fill=fill, width=stroke)
            else:
                draw.line((x0, position, x0, stop), fill=fill, width=stroke)
            position += dash + gap

    run(radius, half, width - radius, half)
    run(radius, height - 1 - half, width - radius, height - 1 - half)
    run(half, radius, half, height - radius)
    run(width - 1 - half, radius, width - 1 - half, height - radius)
    for box, start in (
        ((half, half, radius * 2, radius * 2), 180),
        ((width - radius * 2, half, width - 1 - half, radius * 2), 270),
        ((half, height - radius * 2, radius * 2, height - 1 - half), 90),
        ((width - radius * 2, height - radius * 2, width - 1 - half, height - 1 - half), 0),
    ):
        draw.arc(box, start, start + 90, fill=fill, width=stroke)


def pill(
    text: str,
    *,
    size: int,
    max_width: int,
    fill: tuple[int, int, int] = WHITE,
    ink: tuple[int, int, int] = INK,
    border: tuple[int, int, int] | None = ACCENT,
    dot: tuple[int, int, int] | None = ACCENT,
    badge: str | None = None,
    weight: int = 600,
    alpha: int = 255,
) -> Image.Image:
    """A rounded label. ``badge`` puts a short string (a number) in a filled circle at its
    left; otherwise ``dot`` draws a small filled dot."""
    pad = round(size * 0.55)
    lead = round(size * 1.15) if badge else (round(size * 0.9) if dot else 0)
    fitted = fit_text(
        text,
        max_width=max(max_width - 2 * pad - lead, size),
        max_height=round(size * 1.5) * 2,
        max_size=size,
        min_size=max(12, size // 2),
        weight=weight,
        max_lines=2,
    )
    height = fitted.height + 2 * round(size * 0.35)
    width = min(max_width, fitted.width + 2 * pad + lead)
    radius = min(height // 2, round(size * 0.9))
    base = panel(
        width,
        height,
        radius,
        (*fill, alpha) if alpha < 255 else fill,
        outline=border,
        outline_width=max(2, size // 12) if border else 0,
    )
    draw = ImageDraw.Draw(base)
    text_img = text_layer(fitted, ink)
    base.alpha_composite(text_img, (pad + lead, (height - fitted.height) // 2))
    if badge:
        d = round(size * 0.95)
        circle = Image.new("RGBA", (d * SUPERSAMPLE, d * SUPERSAMPLE), (*ACCENT, 0))
        ImageDraw.Draw(circle).ellipse(
            (0, 0, d * SUPERSAMPLE - 1, d * SUPERSAMPLE - 1), fill=(*ACCENT, 255)
        )
        circle = circle.resize((d, d), Image.Resampling.LANCZOS)
        badge_fit = fit_text(
            badge, max_width=d, max_height=d, max_size=round(size * 0.7), min_size=8, weight=700
        )
        badge_txt = text_layer(badge_fit, WHITE, width=d, align="center")
        circle.alpha_composite(badge_txt, (0, (d - badge_fit.height) // 2))
        base.alpha_composite(circle, (pad, (height - d) // 2))
    elif dot:
        d = round(size * 0.42)
        draw.ellipse((pad, (height - d) // 2, pad + d, (height + d) // 2), fill=(*dot, 255))
    return base


# --- scenes ---------------------------------------------------------------------------


class SceneCompositor:
    """Builds the layers for one scene and draws its frames."""

    def __init__(
        self,
        *,
        template: str,
        content: Mapping[str, Any],
        orientation: str,
        width: int,
        height: int,
        duration_ms: int,
        enter_fade_ms: int = anim.DEFAULT_FADE_IN_MS,
        exit_fade_ms: int = anim.DEFAULT_FADE_OUT_MS,
    ) -> None:
        self.template = template
        self.content = content
        self.orientation = orientation
        self.width = width
        self.height = height
        self.duration_ms = duration_ms
        # How the scene fades in / out at its edges; 0 is a straight cut (see domain.render).
        self.enter_fade_ms = enter_fade_ms
        self.exit_fade_ms = exit_fade_ms
        self.scale = (height if orientation == "horizontal" else width) / 1080
        self.vertical = orientation == "vertical"
        self.arrangement = layout_for(template, orientation).arrangement
        self.layers: list[Layer] = []
        self._background = background(width, height)
        builder = getattr(self, f"_build_{template}", None)
        if builder is None:
            raise ValueError(f"No renderer for template {template!r}.")
        builder()
        if enter_fade_ms == 0:
            # A scene that cuts in has nothing to fade in from: its first content is already in
            # place on its first frame, and everything after it keeps its pacing.
            for layer in self.layers:
                layer.start_ms -= anim.CUT_IN_SHIFT_MS

    # -- helpers --------------------------------------------------------------------

    def u(self, value: float) -> int:
        return max(1, round(value * self.scale))

    def _add(self, image: Image.Image, x: int, y: int, start_ms: float, **kwargs: bool) -> None:
        self.layers.append(Layer.from_rgba(image, x, y, start_ms, **kwargs))

    @property
    def margin_x(self) -> int:
        return self.u(80 if self.vertical else 140)

    @property
    def margin_y(self) -> int:
        return self.u(150 if self.vertical else 96)

    def _string(self, key: str) -> str:
        value = self.content.get(key)
        return value.strip() if isinstance(value, str) else ""

    def _illustration(self) -> tuple[str, list[str]]:
        raw = self.content.get("illustration")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        description = data.get("description")
        labels = data.get("subject_labels")
        return (
            description.strip()
            if isinstance(description, str) and description.strip()
            else "Key idea",
            [label for label in labels if isinstance(label, str)]
            if isinstance(labels, list)
            else [],
        )

    # -- templates ------------------------------------------------------------------

    def _build_title_card(self) -> None:
        w, h, u = self.width, self.height, self.u
        px = u(56 if self.vertical else 80)
        py = u(220 if self.vertical else 80)
        panel_w, panel_h = w - 2 * px, h - 2 * py
        self._add(
            panel(panel_w, panel_h, u(48), ACCENT, gradient_to=ACCENT_DEEP),
            px,
            py,
            0,
            slide=False,
        )
        inner_w = panel_w - 2 * u(70 if self.vertical else 120)
        title = fit_text(
            self._string("title"),
            max_width=inner_w,
            max_height=round(panel_h * 0.5),
            max_size=u(92 if self.vertical else 100),
            min_size=u(40),
            weight=750,
            gap_ratio=0.0,
        )
        subtitle_text = self._string("subtitle")
        subtitle = (
            fit_text(
                subtitle_text,
                max_width=inner_w,
                max_height=round(panel_h * 0.26),
                max_size=u(44),
                min_size=u(24),
                weight=400,
                gap_ratio=0.0,
            )
            if subtitle_text
            else None
        )
        bar_h, gap = u(10), u(34)
        total = bar_h + gap + title.height + ((gap + subtitle.height) if subtitle else 0)
        y = py + (panel_h - total) // 2
        bar = panel(u(120), bar_h, bar_h // 2, WARM)
        self._add(bar, (w - u(120)) // 2, y, 200)
        y += bar_h + gap
        self._add(
            text_layer(title, WHITE, width=inner_w, align="center"),
            px + (panel_w - inner_w) // 2,
            y,
            300,
        )
        y += title.height + gap
        if subtitle:
            self._add(
                text_layer(subtitle, (220, 228, 255), width=inner_w, align="center"),
                px + (panel_w - inner_w) // 2,
                y,
                800,
            )

    def _build_bullet_points(self) -> None:
        w, h, u = self.width, self.height, self.u
        mx, my = self.margin_x, self.margin_y
        content_w = w - 2 * mx
        y = my
        title_text = self._string("title")
        if title_text:
            title = fit_text(
                title_text,
                max_width=content_w,
                max_height=round(h * 0.2),
                max_size=u(76),
                min_size=u(40),
                weight=750,
                gap_ratio=0.0,
            )
            self._add(text_layer(title, INK, width=content_w), mx, y, 0)
            y += title.height + u(16)
            self._add(panel(u(140), u(10), u(5), WARM), mx, y, 150)
            y += u(10) + u(40)
        raw = self.content.get("bullets")
        bullets = [b for b in raw if isinstance(b, str)] if isinstance(raw, list) else []
        region_h = h - my - y
        pad_x, pad_y = u(36), u(24)
        best: tuple[int, list[FittedText], int] | None = None
        for size in range(u(58 if self.vertical else 54), u(28) - 1, -2):
            gap = round(size * (0.85 if self.vertical else 0.6))
            lead = round(size * 0.9)
            fits = [
                fit_text(
                    bullet,
                    max_width=content_w - 2 * pad_x - lead,
                    max_height=10_000,
                    max_size=size,
                    min_size=size,
                    weight=500,
                    gap_ratio=0.0,
                )
                for bullet in bullets
            ]
            total = sum(f.height + 2 * pad_y for f in fits) + gap * (len(fits) - 1)
            best = (size, fits, gap)
            if total <= region_h:
                break
        if best is None:
            return
        size, fits, gap = best
        lead = round(size * 0.9)
        total = sum(f.height + 2 * pad_y for f in fits) + gap * (len(fits) - 1)
        row_y = y + max((region_h - total) // 2, 0)
        starts = anim.item_reveal_starts(self.duration_ms, len(fits), exit_ms=self.exit_fade_ms)
        for fitted, start in zip(fits, starts, strict=True):
            card_h = fitted.height + 2 * pad_y
            card = panel(
                content_w, card_h, u(24), WHITE, outline=(214, 220, 240), outline_width=max(2, u(3))
            )
            d = round(size * 0.36)
            ImageDraw.Draw(card).ellipse(
                (
                    pad_x,
                    pad_y + (fitted.pitch - d) // 2,
                    pad_x + d,
                    pad_y + (fitted.pitch - d) // 2 + d,
                ),
                fill=(*ACCENT, 255),
            )
            card.alpha_composite(text_layer(fitted, INK), (pad_x + lead, pad_y))
            self._add(card, mx, row_y, start)
            row_y += card_h + gap

    # -- illustrated templates: a procedural visual, or a deterministic text fallback -----------
    #
    # ``labelled_diagram``, ``split_text_illustration`` and ``full_bleed_illustration`` may carry
    # a procedural visual (``domain.visual``): one of five diagram forms laid out by
    # ``domain.visual_layout`` and painted by ``visual_scene``. Without one - or if it does not
    # fit its region cleanly (``LayoutOverflow``) - each draws a deliberate text fallback, never
    # an empty panel and never anything that implies a picture exists.

    def _visual_layout(self, region: Rect) -> VisualLayout | None:
        """The scene's visual laid out in ``region``, or ``None`` if it has no valid visual or
        the visual does not fit (the caller then draws the fallback)."""
        from edutoon.domain.visual import visual_of
        from edutoon.domain.visual_layout import LayoutOverflow, layout_visual
        from edutoon.providers.renderer.visual_text import PillowTextMeasurer

        visual = visual_of(self.content)
        if visual is None:
            return None
        try:
            return layout_visual(
                visual,
                orientation=self.orientation,
                region=region,
                short_side=min(self.width, self.height),
                measurer=PillowTextMeasurer(),
            )
        except LayoutOverflow:
            return None

    def _paint_visual(self, layout: VisualLayout) -> None:
        from edutoon.providers.renderer.visual_scene import paint_visual

        starts = anim.item_reveal_starts(self.duration_ms, layout.steps, exit_ms=self.exit_fade_ms)
        units = Units(min(self.width, self.height))
        self.layers.extend(paint_visual(layout, units=units, starts=starts))

    def _idea(self) -> str:
        """The words a text fallback is built on: the caption if there is one, else the
        illustration's description. Never labelled as a picture."""
        caption = self._string("caption")
        if caption:
            return caption
        description, _labels = self._illustration()
        return description

    def _chips(self, top: int, centre_x: int, max_width: int, start: float) -> None:
        """The illustration's subject labels as small outlined pills, centred on ``centre_x``."""
        _description, labels = self._illustration()
        shown = [label for label in labels if label.strip()][:3]
        spacing = self.u(16)
        share = (max_width - spacing * max(0, len(shown) - 1)) // max(1, len(shown))
        images = [
            pill(
                label,
                size=self.u(30),
                max_width=share,
                fill=WHITE,
                ink=INK_MUTED,
                border=ACCENT_LINE,
                dot=None,
                weight=500,
            )
            for label in shown
        ]
        if not images:
            return
        row = sum(i.width for i in images) + spacing * (len(images) - 1)
        x = centre_x - row // 2
        for image in images:
            self._add(image, max(x, self.margin_x // 2), top, start)
            x += image.width + spacing

    def _chips_height(self) -> int:
        _description, labels = self._illustration()
        return (self.u(30) * 2 + self.u(40)) if any(label.strip() for label in labels) else 0

    def _statement(self, x: int, y: int, w: int, h: int, *, start: float = 0) -> None:
        """A deliberate, full-strength statement of the idea - large text on a solid panel with
        an accent bar - occupying ``x, y, w, h``; the subject labels sit beneath it."""
        u = self.u
        self._add(panel(w, h, u(48), ACCENT, gradient_to=ACCENT_DEEP), x, y, start, slide=False)
        inner_w = w - 2 * u(80 if self.vertical else 120)
        chips_h = self._chips_height()
        text = fit_text(
            self._idea(),
            max_width=inner_w,
            max_height=round(h * 0.55),
            max_size=u(92 if self.vertical else 96),
            min_size=u(36),
            weight=700,
            gap_ratio=0.0,
        )
        bar_h, gap = u(10), u(34)
        total = bar_h + gap + text.height + ((gap + chips_h) if chips_h else 0)
        top = y + (h - total) // 2
        self._add(panel(u(120), bar_h, bar_h // 2, WARM), x + (w - u(120)) // 2, top, start + 200)
        top += bar_h + gap
        self._add(
            text_layer(text, WHITE, width=inner_w, align="center"),
            x + (w - inner_w) // 2,
            top,
            start + 300,
        )
        if chips_h:
            self._chips(top + text.height + gap, x + w // 2, inner_w, start + 800)

    def _parts_fallback(self) -> None:
        """``labelled_diagram`` without a usable visual: its labels as numbered parts, honestly
        a list of labelled parts (their positions are not used, and no figure is implied)."""
        raw = self.content.get("labels")
        labels = [
            str(item.get("text", "")).strip()
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, Mapping) and str(item.get("text", "")).strip()
        ]
        w, h, u = self.width, self.height, self.u
        mx, my = self.margin_x, self.margin_y
        if not labels:
            self._statement(u(40), u(40), w - 2 * u(40), h - 2 * u(40))
            return
        content_w = w - 2 * mx
        columns = 1 if self.vertical or len(labels) <= 3 else 2
        gap_x, gap_y = u(60), u(30 if self.vertical else 36)
        column_w = min((content_w - (columns - 1) * gap_x) // columns, u(760))
        size = u(62 if self.vertical else 56)
        starts = anim.item_reveal_starts(self.duration_ms, len(labels), exit_ms=self.exit_fade_ms)
        images = [
            pill(text, size=size, max_width=column_w, badge=str(index + 1), weight=600)
            for index, text in enumerate(labels)
        ]
        cell_w = max(image.width for image in images)  # centre what is actually drawn
        block_w = columns * cell_w + (columns - 1) * gap_x
        left = mx + (content_w - block_w) // 2
        rows = -(-len(images) // columns)
        row_h = [
            max(i.height for i in images[r * columns : (r + 1) * columns]) for r in range(rows)
        ]
        title_h = 0
        _description, subject = self._illustration()
        title = subject[0].strip() if subject and subject[0].strip() else ""
        if title:
            fitted = fit_text(
                title,
                max_width=content_w,
                max_height=round(h * 0.16),
                max_size=u(72),
                min_size=u(36),
                weight=750,
                gap_ratio=0.0,
            )
            self._add(text_layer(fitted, INK, width=content_w), mx, my, 0)
            self._add(panel(u(140), u(10), u(5), WARM), mx, my + fitted.height + u(16), 150)
            title_h = fitted.height + u(16) + u(10) + u(40)
        block_h = sum(row_h) + gap_y * (rows - 1)
        top = my + title_h + max((h - my - (my + title_h) - block_h) // 2, 0)
        for index, image in enumerate(images):
            row, column = divmod(index, columns)
            x = left + column * (cell_w + gap_x)
            y = top + sum(row_h[:row]) + gap_y * row
            self._add(image, x, y, starts[index])

    def _text_collapse(self) -> None:
        """``split_text_illustration`` without a usable visual: the sentence takes the whole
        width (no empty panel beside it), with the subject labels beneath."""
        w, h, u = self.width, self.height, self.u
        mx, my = self.margin_x, self.margin_y
        text_w = min(w - 2 * mx, u(1240))
        chips_h = self._chips_height()
        fitted = fit_text(
            self._string("text"),
            max_width=text_w,
            max_height=h - 2 * my - chips_h - u(60),
            max_size=u(80 if self.vertical else 84),
            min_size=u(32),
            weight=500,
            gap_ratio=0.0,
        )
        total = fitted.height + ((u(50) + chips_h) if chips_h else 0)
        top = (h - total) // 2
        left = (w - text_w) // 2
        self._add(panel(u(10), fitted.height, u(5), WARM), left - u(26), top, 100, slide=False)
        self._add(text_layer(fitted, INK, width=text_w), left, top, 350)
        if chips_h:
            self._chips(top + fitted.height + u(50), w // 2, text_w, 900)

    def _build_labelled_diagram(self) -> None:
        w, h = self.width, self.height
        mx, my = self.margin_x, self.margin_y
        layout = self._visual_layout(Rect(mx, my, w - 2 * mx, h - 2 * my))
        if layout is not None:
            self._paint_visual(layout)
        else:
            self._parts_fallback()

    def _build_split_text_illustration(self) -> None:
        w, h, u = self.width, self.height, self.u
        mx, my = self.margin_x, self.margin_y
        text = self._string("text")
        if self.vertical:
            panel_h = round(h * 0.42)
            layout = self._visual_layout(Rect(mx, my, w - 2 * mx, panel_h))
            if layout is None:
                self._text_collapse()
                return
            self._paint_visual(layout)
            region_y = my + panel_h + u(50)
            region_h = h - my - region_y
            fitted = fit_text(
                text,
                max_width=w - 2 * mx,
                max_height=region_h,
                max_size=u(62),
                min_size=u(30),
                weight=500,
                gap_ratio=0.0,
            )
            self._add(
                text_layer(fitted, INK, width=w - 2 * mx),
                mx,
                region_y + (region_h - fitted.height) // 2,
                350,
            )
            return
        col_w = round((w - 2 * mx) * 0.46)
        px = mx + col_w + u(70)
        layout = self._visual_layout(Rect(px, my, w - mx - px, h - 2 * my))
        if layout is None:
            self._text_collapse()
            return
        fitted = fit_text(
            text,
            max_width=col_w,
            max_height=h - 2 * my,
            max_size=u(66),
            min_size=u(30),
            weight=500,
            gap_ratio=0.0,
        )
        top = (h - fitted.height) // 2
        self._add(panel(u(10), fitted.height, u(5), WARM), mx - u(26), top, 100, slide=False)
        self._add(text_layer(fitted, INK, width=col_w), mx, top, 350)
        self._paint_visual(layout)

    def _build_full_bleed_illustration(self) -> None:
        w, h, u = self.width, self.height, self.u
        mx, my = self.margin_x, self.margin_y
        caption = self._string("caption")
        pill_image = (
            pill(
                caption,
                size=u(40),
                max_width=round(w * (0.86 if self.vertical else 0.66)),
                fill=INK,
                ink=WHITE,
                border=None,
                dot=None,
                alpha=235,
            )
            if caption
            else None
        )
        caption_y = 0
        region_bottom = h - my
        if pill_image is not None:
            caption_y = h - (u(300) if self.vertical else u(170)) - pill_image.height // 2
            region_bottom = min(region_bottom, caption_y - u(28))
        region = Rect(mx, my, w - 2 * mx, region_bottom - my)
        layout = self._visual_layout(region) if region.h > u(300) else None
        if layout is None:
            # The statement card: the caption (or the description) as the whole scene.
            inset = u(40)
            self._statement(inset, inset, w - 2 * inset, h - 2 * inset)
            return
        self._paint_visual(layout)
        if pill_image is not None:
            self._add(pill_image, (w - pill_image.width) // 2, caption_y, 700)

    def _build_formula_or_code(self) -> None:
        w, h, u = self.width, self.height, self.u
        kind = self._string("kind") or "formula"
        text = self.content.get("content")
        text = text.rstrip("\n") if isinstance(text, str) else ""
        label = self._string("label")
        panel_w = round(w * (0.90 if self.vertical else 0.80))
        pad = u(56)
        inner_w = panel_w - 2 * pad
        is_code = kind == "code"
        fitted = fit_text(
            text,
            max_width=inner_w,
            max_height=round(h * 0.5),
            max_size=u(60 if not is_code else 48),
            min_size=u(24),
            weight=450,
            mono=True,
            gap_ratio=0.0,
        )
        badge_img = pill(
            "CODE" if is_code else "FORMULA",
            size=u(26),
            max_width=u(260),
            fill=ACCENT,
            ink=WHITE,
            border=None,
            dot=None,
            weight=700,
        )
        label_fit = (
            fit_text(
                label,
                max_width=inner_w - badge_img.width - u(20),
                max_height=u(60),
                max_size=u(30),
                min_size=u(16),
                weight=500,
                max_lines=1,
            )
            if label
            else None
        )
        header_h = badge_img.height + u(36)
        panel_h = header_h + fitted.height + 2 * pad
        px, py = (w - panel_w) // 2, (h - panel_h) // 2
        self._add(panel(panel_w, panel_h, u(36), CODE_BG), px, py, 0, slide=False)
        self._add(badge_img, px + pad, py + pad, 250)
        if label_fit:
            self._add(
                text_layer(label_fit, CODE_MUTED),
                px + pad + badge_img.width + u(20),
                py + pad + (badge_img.height - label_fit.height) // 2,
                350,
            )
        body_y = py + pad + header_h
        if is_code:
            span = min(2400.0, self.duration_ms * 0.45)
            step = span / max(len(fitted.lines), 1)
            for index, line in enumerate(fitted.lines):
                one = FittedText(
                    face=fitted.face, lines=[line or " "], pitch=fitted.pitch, size=fitted.size
                )
                self._add(
                    text_layer(one, CODE_INK, width=inner_w),
                    px + pad,
                    body_y + index * fitted.pitch,
                    anim.FIRST_ITEM_DELAY_MS + index * step,
                    slide=False,
                )
        else:
            self._add(
                text_layer(fitted, CODE_INK, width=inner_w, align="center"),
                px + pad,
                body_y,
                anim.FIRST_ITEM_DELAY_MS,
                slide=False,
                wipe=True,
            )

    # -- frames ---------------------------------------------------------------------

    def _layer_state(self, layer: Layer, t_ms: float, envelope: float) -> tuple[int, int, int]:
        """(opacity 0-255, vertical offset, revealed width) - everything that decides how a
        layer looks at ``t_ms``."""
        if layer.fade:
            opacity = anim.item_alpha(t_ms, layer.start_ms) * envelope
        else:
            opacity = envelope if t_ms >= layer.start_ms else 0.0
        offset = anim.slide_offset(t_ms, layer.start_ms, self.scale) if layer.slide else 0
        revealed = layer.rgb.width
        if layer.wipe:
            if layer.wipe_ms is None:
                fraction = anim.reveal_fraction(
                    t_ms, self.duration_ms, lead_ms=round(layer.start_ms)
                )
            else:
                fraction = anim.draw_fraction(t_ms, layer.start_ms, layer.wipe_ms)
            vertical = layer.wipe_dir in ("up", "down")
            revealed = round((layer.rgb.height if vertical else layer.rgb.width) * fraction)
        return (round(opacity * 255), offset if opacity > 0 else 0, revealed)

    def _envelope(self, t_ms: float) -> float:
        return anim.scene_envelope(t_ms, self.duration_ms, self.enter_fade_ms, self.exit_fade_ms)

    def state_key(self, t_ms: float) -> tuple[tuple[int, int, int], ...]:
        envelope = self._envelope(t_ms)
        return tuple(self._layer_state(layer, t_ms, envelope) for layer in self.layers)

    def frame(self, t_ms: float) -> Image.Image:
        envelope = self._envelope(t_ms)
        canvas = self._background.copy()
        for layer in self.layers:
            opacity, offset, revealed = self._layer_state(layer, t_ms, envelope)
            if opacity <= 0 or revealed <= 0:
                continue
            rgb, alpha = layer.rgb, layer.alpha
            left, top = layer.x, layer.y + offset
            if layer.wipe:
                width, height = rgb.size
                box: tuple[int, int, int, int] | None = None
                if layer.wipe_dir == "right" and revealed < width:
                    box = (0, 0, revealed, height)
                elif layer.wipe_dir == "left" and revealed < width:
                    box = (width - revealed, 0, width, height)
                    left += width - revealed
                elif layer.wipe_dir == "down" and revealed < height:
                    box = (0, 0, width, revealed)
                elif layer.wipe_dir == "up" and revealed < height:
                    box = (0, height - revealed, width, height)
                    top += height - revealed
                if box is not None:
                    rgb, alpha = rgb.crop(box), alpha.crop(box)
            if opacity < 255:
                lut = [round(v * opacity / 255) for v in range(256)]
                alpha = alpha.point(lut)
            canvas.paste(rgb, (left, top), alpha)
        return canvas
