"""Where everything of a procedural visual goes - pure geometry (rule 1: no I/O, no fonts, no
imports of repositories / providers / services).

**Two phases, so this stays pure.** Text has to be measured to be laid out, and measuring needs a
font. The renderer therefore hands this module a :class:`TextMeasurer` (an injected callable
object, defined here and implemented by ``providers.renderer.visual_text``): phase one asks it how
each piece of text wraps and how big it is at a given size and width; phase two - all of this
module - turns those sizes into rectangles and connectors. A test drives it with a fake
fixed-width measurer and needs no font file.

**Deterministic and integer only.** Every coordinate is an integer pixel. Sizes come from
:class:`Units` (``round(ref * short_side / 1080)`` in integers - the same "reference 1080" idea as
the compositor's ``u()``, without a float). A cycle's ring uses a fixed-point table of integers
(:data:`RING`), never ``sin``/``cos``; a connector's ends are found by integer clipping against a
box's edges. There is no randomness, no clock, no platform-dependent number anywhere.

**Never wrong, or not at all.** A layout that would clip a label, overlap two boxes, leave a
connector short of its box, run a connector through a box, or leave the region is rejected: the
module retries at smaller type (a fixed ladder, :data:`FONT_PERCENTS`, never below each role's
minimum, :data:`ROLE_SPECS`) and, if nothing fits, raises :class:`LayoutOverflow`. The caller then
draws the deterministic fallback. Text is **never** ellipsised, trimmed or split mid-word here -
the measurer reports "does not fit" instead.

**Each orientation is designed, not scaled.** Horizontal and vertical have separate layout
functions per visual type (see :data:`_LAYOUTS`):

======================  ==============================  ===================================
type                    horizontal                      vertical
======================  ==============================  ===================================
``process_flow``        one row (<= 4) or wrapped rows  a single column
``cycle``               a ring                          a column with a return arrow
``hierarchy``           a top-down tree                 an indented outline
``comparison``          columns side by side            bands stacked, one per column
``labelled_components`` hub with parts either side,     subject above, parts below
                        or bands beside the subject
======================  ==============================  ===================================
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final, Literal, Protocol

from edutoon.domain.visual import (
    Comparison,
    Cycle,
    Hierarchy,
    LabelledComponents,
    ProcessFlow,
    Visual,
    VisualNode,
)

Point = tuple[int, int]

# --- integer helpers --------------------------------------------------------------------------


def div_round(numerator: int, denominator: int) -> int:
    """``numerator / denominator`` rounded half away from zero, in integers."""
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    if numerator >= 0:
        return (2 * numerator + denominator) // (2 * denominator)
    return -((2 * -numerator + denominator) // (2 * denominator))


@dataclass(frozen=True, slots=True)
class Units:
    """The 1080-pixel reference scale: ``u(ref)`` is ``ref`` reference pixels at this canvas's
    short side, rounded half up, at least 1 - all integers."""

    short_side: int

    def u(self, ref: int) -> int:
        return max(1, (ref * self.short_side + 540) // 1080)


@dataclass(frozen=True, slots=True)
class Rect:
    """A rectangle by its top-left corner and size. Edges are the lines ``x``, ``x + w``, ``y``,
    ``y + h``: a box "touches" a connector whose end lies on one of them."""

    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def intersects(self, other: Rect) -> bool:
        """Whether the interiors overlap (touching edges do not count)."""
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )

    def contains(self, other: Rect) -> bool:
        return (
            other.x >= self.x
            and other.y >= self.y
            and other.right <= self.right
            and other.bottom <= self.bottom
        )

    def contains_point(self, point: Point) -> bool:
        return self.x <= point[0] <= self.right and self.y <= point[1] <= self.bottom

    def on_border(self, point: Point) -> bool:
        return self.contains_point(point) and (
            point[0] in (self.x, self.right) or point[1] in (self.y, self.bottom)
        )


# --- measuring text (phase one) ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBlock:
    """Text wrapped into ``lines``: ``width`` is the widest line, ``height`` is
    ``pitch * len(lines)``."""

    lines: tuple[str, ...]
    width: int
    height: int
    pitch: int


class TextMeasurer(Protocol):
    def measure(
        self, text: str, *, size: int, weight: int, max_width: int, max_lines: int
    ) -> TextBlock | None:
        """Wrap ``text`` at ``size`` pixels and ``weight`` into at most ``max_lines`` lines of at
        most ``max_width`` pixels, breaking only between words - or return ``None`` if it cannot
        be done (a word wider than the line, or too many lines). Never truncates."""
        ...


# role -> (reference size, smallest reference size, weight)
ROLE_SPECS: Final[dict[str, tuple[int, int, int]]] = {
    "title": (44, 34, 700),
    "label": (36, 30, 600),
    "detail": (28, 24, 400),
    "heading": (38, 30, 700),
    "point": (30, 26, 500),
    "subject": (42, 32, 700),
    "relation": (30, 26, 700),
}
# The type-size ladder: every role is tried at these percentages of its reference size (never
# below its own minimum) before a visual is given up on.
FONT_PERCENTS: Final = (100, 92, 84, 76)

# reference-pixel design constants
PAD_X: Final = 22
PAD_Y: Final = 16
BADGE: Final = 44
BADGE_GAP: Final = 14
TITLE_GAP: Final = 28
MIN_ARROW: Final = 56
RELATION_LABEL: Final = "VS"

# Cycle ring positions: (cos, sin) x 1024 for k = 0..n-1, clockwise from the top. Integer
# literals, so a ring is placed identically on every platform; a test recomputes them.
RING: Final[dict[int, tuple[tuple[int, int], ...]]] = {
    3: ((0, -1024), (887, 512), (-887, 512)),
    4: ((0, -1024), (1024, 0), (0, 1024), (-1024, 0)),
    5: ((0, -1024), (974, -316), (602, 828), (-602, 828), (-974, -316)),
    6: ((0, -1024), (887, -512), (887, 512), (0, 1024), (-887, 512), (-887, -512)),
}

# --- the result (phase two) --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlacedText:
    """Wrapped ``lines`` to draw with their top-left at (``x``, ``y``) in a block ``width`` wide
    (``align`` is ``left`` or ``center`` within that width)."""

    role: str
    lines: tuple[str, ...]
    x: int
    y: int
    width: int
    size: int
    weight: int
    pitch: int
    align: str = "left"


@dataclass(frozen=True, slots=True)
class LayoutBox:
    """A rectangle with its text. ``kind`` is ``node``, ``band``, ``heading``, ``point``,
    ``subject`` or ``relation`` (styling only). ``badge`` is where a step-number disc goes and
    ``number`` its 1-based value; ``step`` is when it is revealed; ``accent`` marks the focus."""

    kind: str
    rect: Rect
    texts: tuple[PlacedText, ...]
    step: int
    number: int | None = None
    badge: Rect | None = None
    accent: bool = False


@dataclass(frozen=True, slots=True)
class LayoutConnector:
    """A polyline. ``arrow`` ends in an arrowhead, ``line`` is plain, ``leader`` ends in a dot."""

    kind: Literal["arrow", "line", "leader"]
    points: tuple[Point, ...]
    step: int


@dataclass(frozen=True, slots=True)
class VisualLayout:
    boxes: tuple[LayoutBox, ...]
    connectors: tuple[LayoutConnector, ...]
    title: PlacedText | None
    steps: int
    bounds: Rect
    font_percent: int


class LayoutOverflow(Exception):  # noqa: N818 - the name the fallback contract uses
    """The visual cannot be laid out cleanly in its region at any allowed type size. Never
    shown to a project's owner: the caller draws the deterministic fallback instead."""


class _FitError(Exception):
    """Internal: this attempt (at this type size) does not fit."""


# --- geometry checks (also the layout's own last word) -----------------------------------------


def crosses_interior(a: Point, b: Point, rect: Rect) -> bool:
    """Whether the segment ``a``-``b`` passes through the inside of ``rect`` (a segment that only
    touches its edge does not). Exact: rational arithmetic, no floats."""
    x0, y0, x1, y1 = rect.x + 1, rect.y + 1, rect.right - 1, rect.bottom - 1
    if x0 > x1 or y0 > y1:
        return False
    t0, t1 = Fraction(0), Fraction(1)
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        r = Fraction(q, p)
        if p < 0:
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            if r < t0:
                return False
            t1 = min(t1, r)
    return t0 <= t1


def check_layout(
    boxes: tuple[LayoutBox, ...], connectors: tuple[LayoutConnector, ...], region: Rect
) -> list[str]:
    """Everything wrong with a layout (empty means clean): a box outside the region or over
    another, a connector outside the region, an end that is not on a box's edge, a connector
    through a box, or a zero-length connector."""
    problems: list[str] = []
    for index, box in enumerate(boxes):
        if not region.contains(box.rect):
            problems.append(f"box {index} ({box.kind}) leaves the region")
        for other_index in range(index + 1, len(boxes)):
            if box.rect.intersects(boxes[other_index].rect):
                problems.append(f"boxes {index} and {other_index} overlap")
    for index, connector in enumerate(connectors):
        points = connector.points
        if len(points) < 2 or all(p == points[0] for p in points):
            problems.append(f"connector {index} has no length")
            continue
        if any(not region.contains_point(p) for p in points):
            problems.append(f"connector {index} leaves the region")
        for end in (points[0], points[-1]):
            if not any(box.rect.on_border(end) for box in boxes):
                problems.append(f"connector {index} has an end that touches no box")
        for a, b in zip(points, points[1:], strict=False):
            for box_index, box in enumerate(boxes):
                if crosses_interior(a, b, box.rect):
                    problems.append(f"connector {index} runs through box {box_index}")
    return problems


def _exit_point(rect: Rect, toward: Point) -> Point:
    """Where the straight line from ``rect``'s centre to ``toward`` leaves ``rect``: on its edge,
    found by integer clipping."""
    cx, cy = rect.cx, rect.cy
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        raise _FitError("a connector between coincident boxes")
    half_w = (rect.right - cx) if dx > 0 else (cx - rect.x)
    half_h = (rect.bottom - cy) if dy > 0 else (cy - rect.y)
    if abs(dx) * half_h >= abs(dy) * half_w:
        x = cx + (half_w if dx > 0 else -half_w)
        y = cy + div_round(dy * half_w, abs(dx))
    else:
        y = cy + (half_h if dy > 0 else -half_h)
        x = cx + div_round(dx * half_h, abs(dy))
    return (x, y)


# --- the machinery each layout uses ------------------------------------------------------------


class _Ctx:
    def __init__(self, units: Units, measurer: TextMeasurer, percent: int, body: Rect) -> None:
        self.units = units
        self.measurer = measurer
        self.percent = percent
        self.body = body

    def u(self, ref: int) -> int:
        return self.units.u(ref)

    def size(self, role: str) -> int:
        base, smallest, _ = ROLE_SPECS[role]
        return self.units.u(max(smallest, base * self.percent // 100))

    def text(self, role: str, value: str, max_width: int, max_lines: int) -> TextBlock:
        if max_width <= 0:
            raise _FitError("no room for text")
        block = self.measurer.measure(
            value,
            size=self.size(role),
            weight=ROLE_SPECS[role][2],
            max_width=max_width,
            max_lines=max_lines,
        )
        if block is None:
            raise _FitError(f"a {role} does not fit")
        return block

    def placed(
        self, role: str, block: TextBlock, x: int, y: int, width: int, align: str = "left"
    ) -> PlacedText:
        return PlacedText(
            role=role,
            lines=block.lines,
            x=x,
            y=y,
            width=width,
            size=self.size(role),
            weight=ROLE_SPECS[role][2],
            pitch=block.pitch,
            align=align,
        )


class _Draft:
    def __init__(self) -> None:
        self.boxes: list[LayoutBox] = []
        self.connectors: list[LayoutConnector] = []

    def connect(
        self, kind: Literal["arrow", "line", "leader"], points: list[Point], step: int
    ) -> None:
        """Add a polyline, dropping repeated points and points in the middle of a straight run."""
        clean: list[Point] = []
        for point in points:
            if not clean or clean[-1] != point:
                clean.append(point)
        straightened = [clean[0]] if clean else []
        for index in range(1, len(clean) - 1):
            before, here, after = straightened[-1], clean[index], clean[index + 1]
            cross = (here[0] - before[0]) * (after[1] - here[1]) - (here[1] - before[1]) * (
                after[0] - here[0]
            )
            dot = (here[0] - before[0]) * (after[0] - here[0]) + (here[1] - before[1]) * (
                after[1] - here[1]
            )
            if cross == 0 and dot > 0:
                continue
            straightened.append(here)
        straightened += clean[-1:] if len(clean) > 1 else []
        self.connectors.append(LayoutConnector(kind=kind, points=tuple(straightened), step=step))

    @property
    def steps(self) -> int:
        used = [b.step for b in self.boxes] + [c.step for c in self.connectors]
        return (max(used) + 1) if used else 1


@dataclass(frozen=True, slots=True)
class _NodeText:
    label: TextBlock
    detail: TextBlock | None
    gap: int

    @property
    def height(self) -> int:
        return self.label.height + (self.gap + self.detail.height if self.detail else 0)


def _lead(ctx: _Ctx, numbered: bool) -> int:
    return ctx.u(BADGE) + ctx.u(BADGE_GAP) if numbered else 0


def _measure_node(ctx: _Ctx, node: VisualNode, box_width: int, numbered: bool) -> _NodeText:
    inner = box_width - 2 * ctx.u(PAD_X) - _lead(ctx, numbered)
    label = ctx.text("label", node.label, inner, 2)
    detail = ctx.text("detail", node.detail, inner, 3) if node.detail else None
    return _NodeText(label=label, detail=detail, gap=ctx.u(8) if detail else 0)


def _node_height(ctx: _Ctx, content: _NodeText, numbered: bool) -> int:
    inner = max(content.height, ctx.u(BADGE) if numbered else 0)
    return 2 * ctx.u(PAD_Y) + inner


def _node_box(
    ctx: _Ctx,
    kind: str,
    rect: Rect,
    content: _NodeText,
    *,
    number: int | None,
    step: int,
    accent: bool,
) -> LayoutBox:
    pad_x = ctx.u(PAD_X)
    lead = _lead(ctx, number is not None)
    text_x = rect.x + pad_x + lead
    inner = rect.w - 2 * pad_x - lead
    top = rect.y + (rect.h - content.height) // 2
    texts = [ctx.placed("label", content.label, text_x, top, inner)]
    if content.detail is not None:
        texts.append(
            ctx.placed(
                "detail", content.detail, text_x, top + content.label.height + content.gap, inner
            )
        )
    badge = None
    if number is not None:
        size = ctx.u(BADGE)
        badge = Rect(rect.x + pad_x, rect.y + (rect.h - size) // 2, size, size)
    return LayoutBox(
        kind=kind,
        rect=rect,
        texts=tuple(texts),
        step=step,
        number=number,
        badge=badge,
        accent=accent,
    )


def _text_box(
    ctx: _Ctx,
    kind: str,
    role: str,
    block: TextBlock,
    rect: Rect,
    *,
    step: int,
    align: str = "center",
    accent: bool = False,
) -> LayoutBox:
    pad_x = ctx.u(PAD_X)
    top = rect.y + (rect.h - block.height) // 2
    text = ctx.placed(role, block, rect.x + pad_x, top, rect.w - 2 * pad_x, align)
    return LayoutBox(kind=kind, rect=rect, texts=(text,), step=step, accent=accent)


def _text_box_height(ctx: _Ctx, block: TextBlock) -> int:
    return block.height + 2 * ctx.u(PAD_Y)


def _stack_height(heights: list[int], gap: int) -> int:
    return sum(heights) + gap * max(0, len(heights) - 1)


def _need(ok: bool, why: str) -> None:
    if not ok:
        raise _FitError(why)


# --- process_flow ------------------------------------------------------------------------------


def _flow_rows(count: int) -> list[int]:
    """Nodes per row for a horizontal flow: up to four in one row, otherwise rows of three."""
    if count <= 4:
        return [count]
    return [3, count - 3]


def _flow_horizontal(ctx: _Ctx, visual: ProcessFlow) -> _Draft:
    body, draft = ctx.body, _Draft()
    rows = _flow_rows(len(visual.nodes))
    columns = rows[0]
    gap_x, gap_y = ctx.u(96), ctx.u(90)
    node_w = min(ctx.u(420), (body.w - (columns - 1) * gap_x) // columns)
    _need(node_w >= ctx.u(200), "the steps are too narrow")

    contents = [_measure_node(ctx, node, node_w, True) for node in visual.nodes]
    row_heights: list[int] = []
    start = 0
    for size in rows:
        row_heights.append(max(_node_height(ctx, c, True) for c in contents[start : start + size]))
        start += size
    total = _stack_height(row_heights, gap_y)
    _need(total <= body.h, "the flow is too tall")

    block_w = columns * node_w + (columns - 1) * gap_x
    left = body.x + (body.w - block_w) // 2
    top = body.y + (body.h - total) // 2
    rects: list[Rect] = []
    index, y = 0, top
    for row, size in enumerate(rows):
        for column in range(size):
            rects.append(Rect(left + column * (node_w + gap_x), y, node_w, row_heights[row]))
            index += 1
        y += row_heights[row] + gap_y

    for index, (rect, content) in enumerate(zip(rects, contents, strict=True)):
        draft.boxes.append(
            _node_box(
                ctx,
                "node",
                rect,
                content,
                number=index + 1,
                step=index,
                accent=visual.focus == index,
            )
        )
    row_of = [row for row, size in enumerate(rows) for _ in range(size)]
    for index in range(1, len(rects)):
        before, after = rects[index - 1], rects[index]
        if row_of[index] == row_of[index - 1]:
            draft.connect("arrow", [(before.right, before.cy), (after.x, before.cy)], index)
        else:
            middle = before.bottom + gap_y // 2
            draft.connect(
                "arrow",
                [
                    (before.cx, before.bottom),
                    (before.cx, middle),
                    (after.cx, middle),
                    (after.cx, after.y),
                ],
                index,
            )
    return draft


def _flow_vertical(ctx: _Ctx, visual: ProcessFlow) -> _Draft:
    body, draft = ctx.body, _Draft()
    node_w = min(body.w, ctx.u(880))
    left = body.x + (body.w - node_w) // 2
    gap = ctx.u(72)
    contents = [_measure_node(ctx, node, node_w, True) for node in visual.nodes]
    heights = [_node_height(ctx, c, True) for c in contents]
    total = _stack_height(heights, gap)
    _need(total <= body.h, "the flow is too tall")
    y = body.y + (body.h - total) // 2
    rects: list[Rect] = []
    for height in heights:
        rects.append(Rect(left, y, node_w, height))
        y += height + gap
    for index, (rect, content) in enumerate(zip(rects, contents, strict=True)):
        draft.boxes.append(
            _node_box(
                ctx,
                "node",
                rect,
                content,
                number=index + 1,
                step=index,
                accent=visual.focus == index,
            )
        )
    for index in range(1, len(rects)):
        before, after = rects[index - 1], rects[index]
        draft.connect("arrow", [(before.cx, before.bottom), (before.cx, after.y)], index)
    return draft


# --- cycle -------------------------------------------------------------------------------------


def _cycle_horizontal(ctx: _Ctx, visual: Cycle) -> _Draft:
    body, draft = ctx.body, _Draft()
    count = len(visual.nodes)
    node_w = min(ctx.u(360), body.w // 3)
    contents = [_measure_node(ctx, node, node_w, True) for node in visual.nodes]
    node_h = max(_node_height(ctx, c, True) for c in contents)
    radius_x = (body.w - node_w) // 2 - 1
    radius_y = (body.h - node_h) // 2 - 1
    _need(radius_x > 0 and radius_y > 0, "the ring has no room")
    centre_x, centre_y = body.x + body.w // 2, body.y + body.h // 2
    rects: list[Rect] = []
    for cos, sin in RING[count]:
        x = centre_x + div_round(radius_x * cos, 1024)
        y = centre_y + div_round(radius_y * sin, 1024)
        rects.append(Rect(x - node_w // 2, y - node_h // 2, node_w, node_h))
    for index, (rect, content) in enumerate(zip(rects, contents, strict=True)):
        draft.boxes.append(
            _node_box(
                ctx,
                "node",
                rect,
                content,
                number=index + 1,
                step=index,
                accent=visual.focus == index,
            )
        )
    for index in range(count):
        source, target = rects[index], rects[(index + 1) % count]
        start = _exit_point(source, (target.cx, target.cy))
        end = _exit_point(target, (source.cx, source.cy))
        draft.connect("arrow", [start, end], index + 1)  # the closing arrow is the last step
    return draft


def _cycle_vertical(ctx: _Ctx, visual: Cycle) -> _Draft:
    body, draft = ctx.body, _Draft()
    gutter = ctx.u(110)
    node_w = body.w - gutter
    gap = ctx.u(72)
    contents = [_measure_node(ctx, node, node_w, True) for node in visual.nodes]
    heights = [_node_height(ctx, c, True) for c in contents]
    total = _stack_height(heights, gap)
    _need(total <= body.h, "the cycle is too tall")
    y = body.y + (body.h - total) // 2
    rects: list[Rect] = []
    for height in heights:
        rects.append(Rect(body.x, y, node_w, height))
        y += height + gap
    for index, (rect, content) in enumerate(zip(rects, contents, strict=True)):
        draft.boxes.append(
            _node_box(
                ctx,
                "node",
                rect,
                content,
                number=index + 1,
                step=index,
                accent=visual.focus == index,
            )
        )
    for index in range(1, len(rects)):
        before, after = rects[index - 1], rects[index]
        draft.connect("arrow", [(before.cx, before.bottom), (before.cx, after.y)], index)
    first, last = rects[0], rects[-1]
    lane = first.right + gutter // 2
    draft.connect(
        "arrow",
        [(last.right, last.cy), (lane, last.cy), (lane, first.cy), (first.right, first.cy)],
        len(rects),
    )
    return draft


# --- hierarchy ---------------------------------------------------------------------------------


def _children(visual: Hierarchy) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {i: [] for i in range(len(visual.nodes))}
    for index, node in enumerate(visual.nodes):
        if node.parent is not None:
            children[node.parent].append(index)
    return children


def _hierarchy_horizontal(ctx: _Ctx, visual: Hierarchy) -> _Draft:
    body, draft = ctx.body, _Draft()
    children = _children(visual)
    slot2: dict[int, int] = {}
    leaves = 0

    def place(index: int) -> None:
        nonlocal leaves
        if not children[index]:
            slot2[index] = 2 * leaves
            leaves += 1
            return
        for child in children[index]:
            place(child)
        slot2[index] = (slot2[children[index][0]] + slot2[children[index][-1]]) // 2

    place(0)
    slot_w = body.w // leaves
    node_w = min(ctx.u(340), slot_w - ctx.u(24))
    _need(node_w >= ctx.u(150), "the tree is too wide")
    left = body.x + (body.w - leaves * slot_w) // 2

    levels = [visual.level_of(i) for i in range(len(visual.nodes))]
    contents = [_measure_node(ctx, node, node_w, False) for node in visual.nodes]
    level_heights = [
        max(_node_height(ctx, contents[i], False) for i in range(len(levels)) if levels[i] == level)
        for level in range(max(levels) + 1)
    ]
    gap_y = ctx.u(80)
    total = _stack_height(level_heights, gap_y)
    _need(total <= body.h, "the tree is too tall")
    level_y = []
    y = body.y + (body.h - total) // 2
    for height in level_heights:
        level_y.append(y)
        y += height + gap_y

    rects: list[Rect] = []
    for index in range(len(visual.nodes)):
        centre = left + (slot2[index] * slot_w) // 2 + slot_w // 2
        rects.append(
            Rect(centre - node_w // 2, level_y[levels[index]], node_w, level_heights[levels[index]])
        )
    for index, node in enumerate(visual.nodes):
        draft.boxes.append(
            _node_box(
                ctx,
                "node",
                rects[index],
                contents[index],
                number=None,
                step=levels[index],
                accent=visual.focus == index,
            )
        )
        if node.parent is not None:
            parent = rects[node.parent]
            middle = parent.bottom + gap_y // 2
            draft.connect(
                "line",
                [
                    (parent.cx, parent.bottom),
                    (parent.cx, middle),
                    (rects[index].cx, middle),
                    (rects[index].cx, rects[index].y),
                ],
                levels[index],
            )
    return draft


def _hierarchy_vertical(ctx: _Ctx, visual: Hierarchy) -> _Draft:
    body, draft = ctx.body, _Draft()
    children = _children(visual)
    order: list[int] = []

    def walk(index: int) -> None:
        order.append(index)
        for child in children[index]:
            walk(child)

    walk(0)
    indent, gap = ctx.u(56), ctx.u(20)
    levels = {i: visual.level_of(i) for i in order}
    widths = {i: body.w - indent * levels[i] for i in order}
    _need(min(widths.values()) >= ctx.u(260), "the outline is too narrow")
    contents = {i: _measure_node(ctx, visual.nodes[i], widths[i], False) for i in order}
    heights = [_node_height(ctx, contents[i], False) for i in order]
    total = _stack_height(heights, gap)
    _need(total <= body.h, "the outline is too tall")
    y = body.y + (body.h - total) // 2
    rects: dict[int, Rect] = {}
    for index, height in zip(order, heights, strict=True):
        rects[index] = Rect(body.x + indent * levels[index], y, widths[index], height)
        y += height + gap
    for step, index in enumerate(order):
        draft.boxes.append(
            _node_box(
                ctx,
                "node",
                rects[index],
                contents[index],
                number=None,
                step=step,
                accent=visual.focus == index,
            )
        )
        parent_index = visual.nodes[index].parent
        if parent_index is not None:
            parent, rect = rects[parent_index], rects[index]
            trunk = parent.x + ctx.u(28)
            draft.connect(
                "line", [(trunk, parent.bottom), (trunk, rect.cy), (rect.x, rect.cy)], step
            )
    return draft


# --- comparison --------------------------------------------------------------------------------


def _comparison_horizontal(ctx: _Ctx, visual: Comparison) -> _Draft:
    body, draft = ctx.body, _Draft()
    count = len(visual.columns)
    gap = ctx.u(120)
    column_w = (body.w - (count - 1) * gap) // count
    inner = column_w - 2 * ctx.u(PAD_X)
    _need(inner >= ctx.u(160), "the columns are too narrow")
    headings = [ctx.text("heading", c.heading, inner, 2) for c in visual.columns]
    heading_h = max(_text_box_height(ctx, b) for b in headings)
    rows = max(len(c.points) for c in visual.columns)
    points = [[ctx.text("point", p, inner, 3) for p in c.points] for c in visual.columns]
    row_h = [
        max(_text_box_height(ctx, points[c][r]) for c in range(count) if r < len(points[c]))
        for r in range(rows)
    ]
    gap_head, gap_row = ctx.u(24), ctx.u(20)
    total = heading_h + gap_head + _stack_height(row_h, gap_row)
    _need(total <= body.h, "the comparison is too tall")
    top = body.y + (body.h - total) // 2

    heading_rects: list[Rect] = []
    for c in range(count):
        x = body.x + c * (column_w + gap)
        heading_rect = Rect(x, top, column_w, heading_h)
        heading_rects.append(heading_rect)
        draft.boxes.append(_text_box(ctx, "heading", "heading", headings[c], heading_rect, step=0))
        y = top + heading_h + gap_head
        for r in range(rows):
            if r < len(points[c]):
                rect = Rect(x, y, column_w, row_h[r])
                draft.boxes.append(
                    _text_box(ctx, "point", "point", points[c][r], rect, step=r + 1, align="left")
                )
            y += row_h[r] + gap_row
    for c in range(count - 1):
        left, right = heading_rects[c], heading_rects[c + 1]
        if visual.relation == "before_after":
            draft.connect("arrow", [(left.right, left.cy), (right.x, left.cy)], 0)
        else:
            marker = ctx.text("relation", RELATION_LABEL, ctx.u(76), 1)
            size = ctx.u(76)
            centre = left.right + gap // 2
            rect = Rect(centre - size // 2, left.cy - size // 2, size, size)
            draft.boxes.append(_text_box(ctx, "relation", "relation", marker, rect, step=0))
    return draft


def _comparison_vertical(ctx: _Ctx, visual: Comparison) -> _Draft:
    body, draft = ctx.body, _Draft()
    count = len(visual.columns)
    inner = body.w - 2 * ctx.u(PAD_X)
    headings = [ctx.text("heading", c.heading, inner, 2) for c in visual.columns]
    points = [[ctx.text("point", p, inner, 3) for p in c.points] for c in visual.columns]
    gap_head, gap_row, gap_band = ctx.u(16), ctx.u(12), ctx.u(96)
    band_heights = []
    for c in range(count):
        heights = [_text_box_height(ctx, headings[c])] + [
            _text_box_height(ctx, b) for b in points[c]
        ]
        band_heights.append(heights)
    total = (
        sum(heights[0] + gap_head + _stack_height(heights[1:], gap_row) for heights in band_heights)
        + (count - 1) * gap_band
    )
    _need(total <= body.h, "the comparison is too tall")
    y = body.y + (body.h - total) // 2
    last_rects: list[Rect] = []
    first_rects: list[Rect] = []
    for c in range(count):
        heights = band_heights[c]
        heading_rect = Rect(body.x, y, body.w, heights[0])
        first_rects.append(heading_rect)
        draft.boxes.append(_text_box(ctx, "heading", "heading", headings[c], heading_rect, step=c))
        y += heights[0] + gap_head
        last = heading_rect
        for r, height in enumerate(heights[1:]):
            rect = Rect(body.x, y, body.w, height)
            draft.boxes.append(
                _text_box(ctx, "point", "point", points[c][r], rect, step=c, align="left")
            )
            last = rect
            y += height + gap_row
        y += gap_band - gap_row
        last_rects.append(last)
    for c in range(count - 1):
        above, below = last_rects[c], first_rects[c + 1]
        if visual.relation == "before_after":
            draft.connect("arrow", [(above.cx, above.bottom), (above.cx, below.y)], c + 1)
        else:
            marker = ctx.text("relation", RELATION_LABEL, ctx.u(76), 1)
            size = ctx.u(76)
            middle = (above.bottom + below.y) // 2
            rect = Rect(body.cx - size // 2, middle - size // 2, size, size)
            draft.boxes.append(_text_box(ctx, "relation", "relation", marker, rect, step=c + 1))
    return draft


# --- labelled_components -----------------------------------------------------------------------


def _components_horizontal(ctx: _Ctx, visual: LabelledComponents) -> _Draft:
    if visual.form == "layers":
        return _layers_horizontal(ctx, visual)
    body, draft = ctx.body, _Draft()
    hub_w = min(ctx.u(420), body.w // 4)
    subject = ctx.text("subject", visual.subject, hub_w - 2 * ctx.u(PAD_X), 3)
    hub_h = max(_text_box_height(ctx, subject), ctx.u(160))
    gap_x, gap_y = ctx.u(110), ctx.u(22)
    side_w = (body.w - hub_w - 2 * gap_x) // 2
    _need(side_w >= ctx.u(240), "the parts are too narrow")
    contents = [_measure_node(ctx, part, side_w, True) for part in visual.parts]
    heights = [_node_height(ctx, c, True) for c in contents]
    sides: dict[str, list[int]] = {"left": [], "right": []}
    for index in range(len(visual.parts)):
        sides["left" if index % 2 == 0 else "right"].append(index)
    for members in sides.values():
        _need(
            _stack_height([heights[i] for i in members], gap_y) <= body.h, "the parts are too tall"
        )

    hub = Rect(body.x + side_w + gap_x, body.y + (body.h - hub_h) // 2, hub_w, hub_h)
    draft.boxes.append(_text_box(ctx, "subject", "subject", subject, hub, step=0))
    for side, members in sides.items():
        total = _stack_height([heights[i] for i in members], gap_y)
        y = body.y + (body.h - total) // 2
        x = body.x if side == "left" else hub.right + gap_x
        for position, index in enumerate(members):
            rect = Rect(x, y, side_w, heights[index])
            y += heights[index] + gap_y
            draft.boxes.append(
                _node_box(
                    ctx,
                    "node",
                    rect,
                    contents[index],
                    number=index + 1,
                    step=index + 1,
                    accent=visual.focus == index,
                )
            )
            reach = hub.y + (hub.h * (2 * position + 1)) // (2 * len(members))
            if side == "left":
                draft.connect("leader", [(rect.right, rect.cy), (hub.x, reach)], index + 1)
            else:
                draft.connect("leader", [(rect.x, rect.cy), (hub.right, reach)], index + 1)
    return draft


def _layers_horizontal(ctx: _Ctx, visual: LabelledComponents) -> _Draft:
    body, draft = ctx.body, _Draft()
    subject_w = min(ctx.u(340), body.w // 4)
    subject = ctx.text("subject", visual.subject, subject_w - 2 * ctx.u(PAD_X), 3)
    subject_h = max(_text_box_height(ctx, subject), ctx.u(140))
    gap_x, gap = ctx.u(110), ctx.u(12)
    band_w = body.w - subject_w - gap_x
    _need(band_w >= ctx.u(320), "the bands are too narrow")
    contents = [_measure_node(ctx, part, band_w, True) for part in visual.parts]
    band_h = max(_node_height(ctx, c, True) for c in contents)
    total = _stack_height([band_h] * len(contents), gap)
    _need(total <= body.h, "the bands are too tall")
    top = body.y + (body.h - total) // 2
    rects = [
        Rect(body.x + subject_w + gap_x, top + i * (band_h + gap), band_w, band_h)
        for i in range(len(contents))
    ]
    middle = rects[len(rects) // 2]
    subject_rect = Rect(body.x, middle.cy - subject_h // 2, subject_w, subject_h)
    draft.boxes.append(_text_box(ctx, "subject", "subject", subject, subject_rect, step=0))
    for index, (rect, content) in enumerate(zip(rects, contents, strict=True)):
        draft.boxes.append(
            _node_box(
                ctx,
                "band",
                rect,
                content,
                number=index + 1,
                step=index + 1,
                accent=visual.focus == index,
            )
        )
    turn = subject_rect.right + gap_x // 2
    draft.connect(
        "leader",
        [
            (subject_rect.right, subject_rect.cy),
            (turn, subject_rect.cy),
            (turn, middle.cy),
            (middle.x, middle.cy),
        ],
        len(rects) // 2 + 1,
    )
    return draft


def _components_vertical(ctx: _Ctx, visual: LabelledComponents) -> _Draft:
    body, draft = ctx.body, _Draft()
    layers = visual.form == "layers"
    subject_w = body.w if layers else min(body.w, ctx.u(640))
    subject = ctx.text("subject", visual.subject, subject_w - 2 * ctx.u(PAD_X), 2)
    subject_h = _text_box_height(ctx, subject)
    indent = 0 if layers else ctx.u(60)
    gap = ctx.u(14) if layers else ctx.u(22)
    space = ctx.u(24) if layers else ctx.u(56)
    part_w = body.w - indent
    contents = [_measure_node(ctx, part, part_w, True) for part in visual.parts]
    heights = [_node_height(ctx, c, True) for c in contents]
    total = subject_h + space + _stack_height(heights, gap)
    _need(total <= body.h, "the parts are too tall")
    top = body.y + (body.h - total) // 2
    subject_rect = Rect(body.x, top, subject_w, subject_h)
    draft.boxes.append(_text_box(ctx, "subject", "subject", subject, subject_rect, step=0))
    y = subject_rect.bottom + space
    trunk = body.x + ctx.u(30)
    for index, (content, height) in enumerate(zip(contents, heights, strict=True)):
        rect = Rect(body.x + indent, y, part_w, height)
        y += height + gap
        draft.boxes.append(
            _node_box(
                ctx,
                "band" if layers else "node",
                rect,
                content,
                number=index + 1,
                step=index + 1,
                accent=visual.focus == index,
            )
        )
        if not layers:
            draft.connect(
                "leader",
                [(trunk, subject_rect.bottom), (trunk, rect.cy), (rect.x, rect.cy)],
                index + 1,
            )
    return draft


# --- the entry point ---------------------------------------------------------------------------

_Layout = Callable[[_Ctx, Any], _Draft]

_LAYOUTS: Final[dict[tuple[str, str], _Layout]] = {
    ("process_flow", "horizontal"): _flow_horizontal,
    ("process_flow", "vertical"): _flow_vertical,
    ("cycle", "horizontal"): _cycle_horizontal,
    ("cycle", "vertical"): _cycle_vertical,
    ("hierarchy", "horizontal"): _hierarchy_horizontal,
    ("hierarchy", "vertical"): _hierarchy_vertical,
    ("comparison", "horizontal"): _comparison_horizontal,
    ("comparison", "vertical"): _comparison_vertical,
    ("labelled_components", "horizontal"): _components_horizontal,
    ("labelled_components", "vertical"): _components_vertical,
}


def _bounds(
    boxes: tuple[LayoutBox, ...], connectors: tuple[LayoutConnector, ...], fallback: Rect
) -> Rect:
    xs = [b.rect.x for b in boxes] + [b.rect.right for b in boxes]
    ys = [b.rect.y for b in boxes] + [b.rect.bottom for b in boxes]
    for connector in connectors:
        xs += [p[0] for p in connector.points]
        ys += [p[1] for p in connector.points]
    if not xs:
        return fallback
    return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def layout_visual(
    visual: Visual,
    *,
    orientation: str,
    region: Rect,
    short_side: int,
    measurer: TextMeasurer,
) -> VisualLayout:
    """Lay ``visual`` out inside ``region`` for ``orientation`` on a canvas whose short side is
    ``short_side`` pixels (1080 at full size, 540 for a review render).

    Tries each type size in :data:`FONT_PERCENTS` and returns the first layout that is clean
    (:func:`check_layout`); raises :class:`LayoutOverflow` if none is. Pure: the same inputs and
    a measurer that answers the same way always give the same layout.
    """
    layout = _LAYOUTS.get((visual.type, orientation))
    if layout is None:
        raise LayoutOverflow(f"No layout for {visual.type!r} in {orientation!r}.")
    units = Units(short_side)
    reason = "nothing was tried"
    for percent in FONT_PERCENTS:
        body = region
        title_text: PlacedText | None = None
        try:
            probe = _Ctx(units, measurer, percent, region)
            if visual.title:
                block = probe.text("title", visual.title, region.w, 2)
                title_text = probe.placed("title", block, region.x, region.y, region.w, "center")
                used = block.height + units.u(TITLE_GAP)
                body = Rect(region.x, region.y + used, region.w, region.h - used)
                _need(body.h > units.u(200), "no room under the title")
            ctx = _Ctx(units, measurer, percent, body)
            draft = layout(ctx, visual)
            boxes, connectors = tuple(draft.boxes), tuple(draft.connectors)
            problems = check_layout(boxes, connectors, region)
            if title_text is not None and any(b.rect.y < body.y for b in boxes):
                problems.append("a box rises into the title")
            if problems:
                raise _FitError(problems[0])
            return VisualLayout(
                boxes=boxes,
                connectors=connectors,
                title=title_text,
                steps=draft.steps,
                bounds=_bounds(boxes, connectors, region),
                font_percent=percent,
            )
        except _FitError as exc:
            reason = str(exc)
    raise LayoutOverflow(reason)
