"""``domain/visual_layout.py`` - the pure geometry of procedural visuals (Phase 11).

Driven with a fake fixed-width measurer, so no font is involved. What is proved, for every visual
type, in both orientations, in the full and the half region, at full size and at review-render
size: nothing overlaps, nothing leaves the region, no connector is detached or runs through a box,
no text is cut or smaller than its role's minimum, every coordinate is an integer, and the same
input always gives the same layout. The structural tests then pin down that each orientation is a
*different design* (a row is not a column), not one design scaled.
"""

from __future__ import annotations

import math

import pytest

from edutoon.domain import visual as v
from edutoon.domain import visual_layout as vl
from tests.visual_corpus import (
    comparison,
    components,
    cycle,
    flow,
    node,
    text,
    tree,
    valid_examples,
)
from tests.visual_layout_helpers import (
    FixedMeasurer,
    all_text,
    assert_clean,
    region_for,
)

ORIENTATIONS = ["horizontal", "vertical"]


def lay(spec, orientation="horizontal", size="full", short_side=1080, measurer=None):
    return vl.layout_visual(
        v.parse_visual(spec),
        orientation=orientation,
        region=region_for(orientation, size, short_side),
        short_side=short_side,
        measurer=measurer or FixedMeasurer(),
    )


# --- integer helpers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "d", "want"), [(7, 2, 4), (-7, 2, -4), (5, 3, 2), (-5, 3, -2), (0, 9, 0), (6, -4, -2)]
)
def test_div_round_is_half_away_from_zero(n, d, want):
    assert vl.div_round(n, d) == want


@pytest.mark.parametrize(
    ("short_side", "ref", "want"),
    [(1080, 36, 36), (540, 36, 18), (540, 35, 18), (540, 1, 1), (1080, 1, 1), (1920, 100, 178)],
)
def test_units_scale_in_integers_and_never_below_one(short_side, ref, want):
    assert vl.Units(short_side).u(ref) == want


def test_the_ring_table_is_the_rounded_unit_circle_clockwise_from_the_top():
    for count, points in vl.RING.items():
        assert len(points) == count
        for k, (cos, sin) in enumerate(points):
            angle = math.radians(-90 + 360 * k / count)
            assert abs(cos - round(1024 * math.cos(angle))) <= 1
            assert abs(sin - round(1024 * math.sin(angle))) <= 1
    assert vl.RING[4] == ((0, -1024), (1024, 0), (0, 1024), (-1024, 0))


def test_a_ring_exists_for_every_cycle_size():
    assert set(vl.RING) == set(range(v.CYCLE_MIN_NODES, v.CYCLE_MAX_NODES + 1))


# --- geometry checks ---------------------------------------------------------------------------


def test_a_segment_through_a_box_crosses_it_but_one_that_only_touches_does_not():
    box = vl.Rect(100, 100, 200, 100)
    assert vl.crosses_interior((50, 150), (350, 150), box)
    assert vl.crosses_interior((150, 50), (150, 250), box)
    assert vl.crosses_interior((60, 60), (340, 240), box)
    assert not vl.crosses_interior((0, 150), (100, 150), box)  # ends on the left edge
    assert not vl.crosses_interior((300, 150), (400, 150), box)  # starts on the right edge
    assert not vl.crosses_interior((100, 0), (100, 300), box)  # runs along an edge
    assert not vl.crosses_interior((0, 50), (400, 50), box)  # passes above


def test_check_layout_reports_each_kind_of_fault():
    region = vl.Rect(0, 0, 1000, 500)

    def box(x, y, w, h):
        return vl.LayoutBox(kind="node", rect=vl.Rect(x, y, w, h), texts=(), step=0)

    clean = (box(50, 50, 200, 100), box(400, 50, 200, 100))
    joined = vl.LayoutConnector("arrow", ((250, 100), (400, 100)), 1)
    assert vl.check_layout(clean, (joined,), region) == []
    assert any(
        "overlap" in p
        for p in vl.check_layout((box(0, 0, 100, 100), box(50, 50, 100, 100)), (), region)
    )
    assert any(
        "leaves the region" in p for p in vl.check_layout((box(900, 0, 200, 100),), (), region)
    )
    detached = vl.LayoutConnector("arrow", ((260, 100), (390, 100)), 1)
    assert any("touches no box" in p for p in vl.check_layout(clean, (detached,), region))
    through = vl.LayoutConnector("line", ((250, 100), (500, 100)), 1)
    assert any("runs through" in p for p in vl.check_layout(clean, (through,), region))
    assert any(
        "no length" in p
        for p in vl.check_layout(
            clean, (vl.LayoutConnector("line", ((250, 100), (250, 100)), 1),), region
        )
    )
    outside = vl.LayoutConnector("line", ((250, 100), (250, 700)), 1)
    assert any("leaves the region" in p for p in vl.check_layout(clean, (outside,), region))


# --- every valid example, both orientations, both regions, both sizes --------------------------


CASES = [
    (name, spec, orientation, size, short_side)
    for name, spec in valid_examples()
    for orientation in ORIENTATIONS
    for size in ("full", "half")
    for short_side in (1080, 540)
]


@pytest.mark.parametrize(
    ("name", "spec", "orientation", "size", "short_side"),
    CASES,
    ids=[f"{n}-{o}-{s}-{q}" for n, _sp, o, s, q in CASES],
)
def test_a_layout_is_clean_or_it_overflows_and_never_something_in_between(
    name, spec, orientation, size, short_side
):
    if size == "half" and not v.fits_region(v.parse_visual(spec), "half"):
        pytest.skip("too large for a half region by design (rejected at storyboard time)")
    try:
        layout = lay(spec, orientation, size, short_side)
    except vl.LayoutOverflow:
        return  # allowed: the caller draws the deterministic fallback
    assert_clean(layout, region_for(orientation, size, short_side), short_side)


def test_typical_visuals_fit_in_the_full_region_in_both_orientations():
    typical = [
        flow(3),
        flow(4),
        flow(6),
        cycle(4),
        cycle(6),
        tree([None, 0, 0, 1, 1]),
        comparison(2, 3),
        comparison(3, 2),
        components(4),
        components(5, "layers"),
    ]
    for spec in typical:
        for orientation in ORIENTATIONS:
            assert_clean(lay(spec, orientation), region_for(orientation))


def test_the_same_input_always_gives_the_same_layout():
    for _name, spec in valid_examples():
        for orientation in ORIENTATIONS:
            try:
                first = lay(spec, orientation)
            except vl.LayoutOverflow:
                continue
            assert lay(spec, orientation) == first


def test_no_text_is_ever_cut_the_words_come_back_exactly_as_written():
    spec = {
        "type": "process_flow",
        "title": "The whole story of it",
        "nodes": [
            node("Light hits the leaf", "and is absorbed by chlorophyll"),
            node("Water is split"),
            node("Sugar is made", "and stored as starch"),
        ],
    }
    for orientation in ORIENTATIONS:
        layout = lay(spec, orientation)
        shown = sorted(all_text(layout))
        assert shown == sorted(
            [
                "The whole story of it",
                "Light hits the leaf",
                "and is absorbed by chlorophyll",
                "Water is split",
                "Sugar is made",
                "and stored as starch",
            ]
        )


# --- process_flow ------------------------------------------------------------------------------


def test_a_horizontal_flow_of_up_to_four_steps_is_one_row_left_to_right():
    for count in (2, 3, 4):
        layout = lay(flow(count))
        rects = [b.rect for b in layout.boxes]
        assert len({r.y for r in rects}) == 1
        assert [r.x for r in rects] == sorted(r.x for r in rects)
        assert len(layout.connectors) == count - 1
        assert all(c.kind == "arrow" and len(c.points) == 2 for c in layout.connectors)
        assert all(c.points[0][1] == c.points[1][1] for c in layout.connectors)  # level


def test_five_and_six_steps_wrap_into_rows_of_three_with_an_elbow():
    five, six = lay(flow(5)), lay(flow(6))
    for layout, second_row in ((five, 2), (six, 3)):
        ys = sorted({b.rect.y for b in layout.boxes})
        assert len(ys) == 2
        assert sum(1 for b in layout.boxes if b.rect.y == ys[1]) == second_row
        elbows = [c for c in layout.connectors if len(c.points) == 4]
        assert len(elbows) == 1
        wrap = elbows[0]
        assert wrap.points[0][0] == wrap.points[1][0] and wrap.points[2][0] == wrap.points[3][0]
        assert wrap.points[1][1] == wrap.points[2][1]
        assert wrap.points[3][1] > wrap.points[0][1]  # it goes down to the next row


def test_a_vertical_flow_is_a_single_column_top_to_bottom():
    layout = lay(flow(5), "vertical")
    rects = [b.rect for b in layout.boxes]
    assert len({r.x for r in rects}) == 1 and len({r.w for r in rects}) == 1
    assert [r.y for r in rects] == sorted(r.y for r in rects)
    assert all(c.points[0][0] == c.points[1][0] for c in layout.connectors)  # straight down
    assert all(c.points[1][1] > c.points[0][1] for c in layout.connectors)


def test_flow_steps_are_numbered_in_order_and_revealed_one_per_step():
    layout = lay(flow(4))
    assert [b.number for b in layout.boxes] == [1, 2, 3, 4]
    assert [b.step for b in layout.boxes] == [0, 1, 2, 3]
    assert [c.step for c in layout.connectors] == [1, 2, 3]  # each arrow with the step it leads to
    assert layout.steps == 4
    assert all(b.badge is not None for b in layout.boxes)


def test_the_focus_step_is_marked_as_the_accent():
    layout = lay(flow(4, focus=2))
    assert [b.accent for b in layout.boxes] == [False, False, True, False]
    assert not any(b.accent for b in lay(flow(4)).boxes)


def test_a_title_sits_above_everything_and_the_body_uses_the_rest():
    layout = lay(flow(3, title="Making sugar"))
    assert layout.title is not None and layout.title.align == "center"
    assert layout.title.lines == ("Making sugar",)
    assert min(b.rect.y for b in layout.boxes) > layout.title.y


# --- cycle -------------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [3, 4, 5, 6])
def test_a_horizontal_cycle_is_a_ring_with_a_closing_arrow(count):
    layout = lay(cycle(count))
    assert len(layout.boxes) == count and len(layout.connectors) == count
    assert layout.steps == count + 1  # the loop closes last
    assert layout.connectors[-1].step == count
    top = min(layout.boxes, key=lambda b: b.rect.y)
    assert top.number == 1, "the first step is at the top"
    centres = [(b.rect.cx, b.rect.cy) for b in layout.boxes]
    cx = sum(x for x, _ in centres) / count
    assert max(abs(x - cx) for x, _ in centres) > 300, "the ring is wide, not a column"
    assert len({b.rect.y for b in layout.boxes}) > 1 and len({b.rect.x for b in layout.boxes}) > 1


def test_a_four_step_ring_has_a_step_at_each_side_and_goes_clockwise():
    layout = lay(cycle(4))
    by_number = {b.number: b.rect for b in layout.boxes}
    assert by_number[1].cy < by_number[2].cy < by_number[3].cy
    assert by_number[2].cx > by_number[1].cx > by_number[4].cx
    assert by_number[3].cx == by_number[1].cx


def test_a_vertical_cycle_is_a_column_with_a_return_arrow_down_the_side():
    layout = lay(cycle(4), "vertical")
    rects = [b.rect for b in layout.boxes]
    assert len({r.x for r in rects}) == 1
    assert [r.y for r in rects] == sorted(r.y for r in rects)
    assert len(layout.connectors) == 4  # three down arrows and one return
    return_arrow = layout.connectors[-1]
    assert len(return_arrow.points) == 4
    right_edge = rects[0].x + rects[0].w
    assert return_arrow.points[1][0] > right_edge, "the return lane is beside the column"
    assert return_arrow.points[0][1] > return_arrow.points[-1][1], "it climbs back up"
    assert return_arrow.step == 4 and layout.steps == 5


# --- hierarchy ---------------------------------------------------------------------------------


def test_a_horizontal_tree_puts_each_level_in_its_own_row_and_centres_parents():
    layout = lay(tree([None, 0, 0, 1, 1, 2]))
    rects = [b.rect for b in layout.boxes]
    rows = {}
    for index, r in enumerate(rects):
        rows.setdefault(r.y, []).append(index)
    assert len(rows) == 3
    levels = [sorted(members) for _y, members in sorted(rows.items())]
    assert levels == [[0], [1, 2], [3, 4, 5]]
    children_of_1 = [rects[3].cx, rects[4].cx]
    assert abs(rects[1].cx - (min(children_of_1) + max(children_of_1)) // 2) <= 1
    assert all(len(c.points) in (2, 4) and c.kind == "line" for c in layout.connectors)
    assert len(layout.connectors) == 5
    assert [b.step for b in layout.boxes] == [0, 1, 1, 2, 2, 2]


def test_a_vertical_tree_is_an_indented_outline_in_depth_first_order():
    layout = lay(tree([None, 0, 1, 0, 3]), "vertical")
    rects = [b.rect for b in layout.boxes]
    assert [r.y for r in rects] == sorted(r.y for r in rects)
    indents = [r.x - rects[0].x for r in rects]
    assert indents[0] == 0 and indents[1] > 0 and indents[2] > indents[1]
    assert indents[3] == indents[1] and indents[4] == indents[2]
    order = [b.step for b in layout.boxes]
    assert order == sorted(order)
    for connector in layout.connectors:  # elbows: down the parent's trunk, then across
        assert len(connector.points) == 3 and connector.points[0][0] == connector.points[1][0]


def test_a_hierarchy_shows_no_numbers_and_draws_plain_lines():
    for orientation in ORIENTATIONS:
        layout = lay(tree([None, 0, 0]), orientation)
        assert all(b.number is None and b.badge is None for b in layout.boxes)
        assert all(c.kind == "line" for c in layout.connectors)


# --- comparison --------------------------------------------------------------------------------


def test_a_horizontal_comparison_is_columns_side_by_side_with_aligned_rows():
    layout = lay(comparison(3, 3))
    headings = [b for b in layout.boxes if b.kind == "heading"]
    points = [b for b in layout.boxes if b.kind == "point"]
    assert len({h.rect.y for h in headings}) == 1
    assert [h.rect.x for h in headings] == sorted(h.rect.x for h in headings)
    assert len(points) == 9
    for row in range(3):
        assert len({p.rect.y for p in points[row::3]} | {points[row].rect.y}) >= 1
    ys = sorted({p.rect.y for p in points})
    assert len(ys) == 3, "rows line up across the columns"
    assert sum(1 for b in layout.boxes if b.kind == "relation") == 2  # a VS between each pair
    assert [b.step for b in headings] == [0, 0, 0]
    assert sorted({p.step for p in points}) == [1, 2, 3]


def test_before_after_draws_an_arrow_between_the_two_headings_instead_of_a_versus():
    layout = lay(comparison(2, 2, "before_after"))
    assert not any(b.kind == "relation" for b in layout.boxes)
    [arrow] = layout.connectors
    assert arrow.kind == "arrow" and arrow.points[0][1] == arrow.points[1][1]
    assert arrow.points[1][0] > arrow.points[0][0]


def test_a_vertical_comparison_stacks_a_band_per_column():
    layout = lay(comparison(3, 2), "vertical")
    headings = [b for b in layout.boxes if b.kind == "heading"]
    assert len(headings) == 3
    assert [h.rect.y for h in headings] == sorted(h.rect.y for h in headings)
    assert len({b.rect.x for b in layout.boxes if b.kind != "relation"}) == 1
    assert len({b.rect.w for b in layout.boxes if b.kind != "relation"}) == 1
    assert [b.step for b in headings] == [0, 1, 2]
    assert sum(1 for b in layout.boxes if b.kind == "relation") == 2


def test_a_vertical_before_after_points_down_from_the_first_band_to_the_second():
    layout = lay(comparison(2, 2, "before_after"), "vertical")
    [arrow] = layout.connectors
    assert arrow.points[0][0] == arrow.points[1][0]
    assert arrow.points[1][1] > arrow.points[0][1]


# --- labelled_components -----------------------------------------------------------------------


def test_a_horizontal_hub_puts_the_subject_in_the_middle_with_parts_alternating_either_side():
    layout = lay(components(5))
    [hub] = [b for b in layout.boxes if b.kind == "subject"]
    parts = [b for b in layout.boxes if b.kind == "node"]
    left = [p for p in parts if p.rect.cx < hub.rect.cx]
    right = [p for p in parts if p.rect.cx > hub.rect.cx]
    assert [p.number for p in left] == [1, 3, 5] and [p.number for p in right] == [2, 4]
    assert abs(hub.rect.cx - (140 + 1640 // 2)) <= 2
    assert len(layout.connectors) == 5 and all(c.kind == "leader" for c in layout.connectors)
    for connector in layout.connectors:
        end = connector.points[-1]
        assert end[0] in (hub.rect.x, hub.rect.x + hub.rect.w)
        assert hub.rect.y <= end[1] <= hub.rect.y + hub.rect.h
    assert hub.step == 0 and sorted(p.step for p in parts) == [1, 2, 3, 4, 5]


def test_horizontal_layers_are_bands_beside_the_subject():
    layout = lay(components(4, "layers"))
    bands = [b for b in layout.boxes if b.kind == "band"]
    [subject] = [b for b in layout.boxes if b.kind == "subject"]
    assert len(bands) == 4 and len({b.rect.x for b in bands}) == 1
    assert len({b.rect.h for b in bands}) == 1
    assert all(b.rect.x > subject.rect.x + subject.rect.w for b in bands)
    assert [b.rect.y for b in bands] == sorted(b.rect.y for b in bands)
    assert len(layout.connectors) == 1


def test_a_vertical_hub_has_the_subject_above_and_the_parts_below_it():
    layout = lay(components(4), "vertical")
    [subject] = [b for b in layout.boxes if b.kind == "subject"]
    parts = [b for b in layout.boxes if b.kind == "node"]
    assert all(p.rect.y >= subject.rect.y + subject.rect.h for p in parts)
    assert len({p.rect.x for p in parts}) == 1 and parts[0].rect.x > subject.rect.x
    assert len(layout.connectors) == 4
    for connector in layout.connectors:
        assert len(connector.points) == 3 and connector.points[0][0] == connector.points[1][0]


def test_vertical_layers_are_bands_full_width_under_the_subject_with_no_connectors():
    layout = lay(components(3, "layers"), "vertical")
    bands = [b for b in layout.boxes if b.kind == "band"]
    [subject] = [b for b in layout.boxes if b.kind == "subject"]
    assert layout.connectors == ()
    assert all(b.rect.w == subject.rect.w for b in bands)
    assert all(b.rect.y > subject.rect.y for b in bands)


# --- the two orientations are different designs, not one scaled --------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        flow(4),
        flow(6),
        cycle(4),
        tree([None, 0, 0, 1]),
        comparison(3, 2),
        components(4),
        components(4, "layers"),
    ],
    ids=["flow4", "flow6", "cycle4", "tree", "comparison", "hub", "layers"],
)
def test_horizontal_and_vertical_layouts_are_structurally_different(spec):
    horizontal, vertical = lay(spec, "horizontal"), lay(spec, "vertical")
    h_rects = [b.rect for b in horizontal.boxes]
    v_rects = [b.rect for b in vertical.boxes]

    # a landscape arrangement spreads sideways more than it stacks; a portrait one the reverse
    def spread(rects):
        return (
            max(r.x + r.w for r in rects) - min(r.x for r in rects),
            max(r.y + r.h for r in rects) - min(r.y for r in rects),
        )

    hw, hh = spread(h_rects)
    vw, vh = spread(v_rects)
    assert hw / max(hh, 1) > vw / max(vh, 1)
    assert h_rects != v_rects
    if horizontal.connectors or vertical.connectors:
        assert [c.points for c in horizontal.connectors] != [c.points for c in vertical.connectors]


def test_scaling_the_horizontal_layout_would_not_give_the_vertical_one():
    horizontal, vertical = lay(flow(4), "horizontal"), lay(flow(4), "vertical")
    assert len({b.rect.y for b in horizontal.boxes}) == 1  # a row
    assert len({b.rect.x for b in vertical.boxes}) == 1  # a column
    assert len({b.rect.x for b in horizontal.boxes}) == 4
    assert len({b.rect.y for b in vertical.boxes}) == 4


# --- overflow, and the type-size ladder --------------------------------------------------------


def test_a_visual_that_cannot_fit_at_any_size_raises_layout_overflow():
    huge = flow(6, title="A title")
    with pytest.raises(vl.LayoutOverflow):
        vl.layout_visual(
            v.parse_visual(huge),
            orientation="horizontal",
            region=vl.Rect(0, 0, 300, 150),
            short_side=1080,
            measurer=FixedMeasurer(),
        )


def test_a_word_wider_than_its_box_is_an_overflow_never_a_split_or_a_cut():
    spec = {"type": "process_flow", "nodes": [node("x" * 28), node("y")]}
    with pytest.raises(vl.LayoutOverflow):
        lay(spec, "horizontal", measurer=FixedMeasurer(width_percent=300))


def test_it_retries_at_smaller_type_and_reports_which_size_it_used():
    spec = flow(4)
    roomy = lay(spec)
    assert roomy.font_percent == vl.FONT_PERCENTS[0]  # as big as the ladder allows
    cramped = lay(spec, measurer=FixedMeasurer(max_size=vl.Units(1080).u(33)))  # 36 px does not fit
    assert cramped.font_percent < roomy.font_percent
    assert_clean(cramped, region_for("horizontal"))
    for box in cramped.boxes:
        for placed in box.texts:
            assert placed.size >= vl.Units(1080).u(vl.ROLE_SPECS[placed.role][1])


def test_text_is_never_drawn_below_a_roles_minimum_even_if_that_means_overflow():
    with pytest.raises(vl.LayoutOverflow):
        lay(flow(4), measurer=FixedMeasurer(max_size=vl.Units(1080).u(20)))


def test_the_ladder_is_the_documented_one_and_every_role_has_a_floor_below_its_size():
    assert vl.FONT_PERCENTS == (140, 125, 110, 100, 92, 84, 76)
    for role, (base, smallest, weight) in vl.ROLE_SPECS.items():
        assert smallest <= base and 100 <= weight <= 900, role
    assert vl.ROLE_SPECS["label"][1] >= 30 and vl.ROLE_SPECS["detail"][1] >= 24


def test_a_region_too_short_for_the_title_and_the_body_overflows():
    with pytest.raises(vl.LayoutOverflow):
        vl.layout_visual(
            v.parse_visual(flow(3, title="Title")),
            orientation="horizontal",
            region=vl.Rect(0, 0, 1640, 230),
            short_side=1080,
            measurer=FixedMeasurer(),
        )


def test_the_measurer_is_asked_at_the_roles_size_and_weight():
    seen = []

    class Spy(FixedMeasurer):
        def measure(self, text, *, size, weight, max_width, max_lines):
            seen.append((size, weight, max_lines))
            return super().measure(
                text, size=size, weight=weight, max_width=max_width, max_lines=max_lines
            )

    lay(flow(2, title="T"), measurer=Spy())
    sizes = {size for size, _w, _l in seen}
    top = vl.FONT_PERCENTS[0]  # the first rung the ladder tries
    assert vl.Units(1080).u(44 * top // 100) in sizes  # the title role's reference size
    assert vl.Units(1080).u(36 * top // 100) in sizes  # the label role's
    assert {weight for _s, weight, _l in seen} == {700, 600}


# --- a big, varied sweep -----------------------------------------------------------------------


def test_a_sweep_of_shapes_and_label_lengths_is_always_clean_or_an_overflow():
    labels = ["Sun", "Light energy", "Carbon dioxide gas", text(28, "w"), "A b c d e f g h i j"]
    details = [None, "short", "a somewhat longer supporting detail line"]
    cleaned = overflowed = 0
    for count in (2, 3, 4, 5, 6):
        for label in labels:
            for detail in details:
                spec = {
                    "type": "process_flow",
                    "nodes": [node(label, detail) for _ in range(count)],
                }
                if count * (len(label) + len(detail or "")) > v.TOTAL_TEXT_MAX_CHARS:
                    continue
                for orientation in ORIENTATIONS:
                    try:
                        layout = lay(spec, orientation)
                    except vl.LayoutOverflow:
                        overflowed += 1
                        continue
                    assert_clean(layout, region_for(orientation))
                    cleaned += 1
    assert cleaned > overflowed > -1
    assert cleaned >= 40


def test_labels_and_details_survive_in_full_for_every_shape():
    for _name, spec in valid_examples():
        parsed = v.parse_visual(spec)
        for orientation in ORIENTATIONS:
            try:
                layout = lay(spec, orientation)
            except vl.LayoutOverflow:
                continue
            shown = " ".join(all_text(layout))
            words = {w for item in nodes_text(parsed) for w in item.split()}
            assert all(word in shown.split() for word in words)


def nodes_text(parsed) -> list[str]:
    out: list[str] = []
    for attr in ("nodes", "parts"):
        for n in getattr(parsed, attr, ()):
            out += [n.label] + ([n.detail] if n.detail else [])
    for column in getattr(parsed, "columns", ()):
        out += [column.heading, *column.points]
    if getattr(parsed, "subject", None):
        out.append(parsed.subject)
    if parsed.title:
        out.append(parsed.title)
    return out
