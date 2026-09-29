"""``domain/visual.py`` - the closed, bounded procedural visual specification (Phase 11).

Pure functions. The corpus (``tests/visual_corpus.py``) is generated from the domain's own limit
constants, so every boundary - the smallest and largest valid size of each list, one item beyond,
the longest valid string and one character more - is exercised for all five visual types.
"""

from __future__ import annotations

import copy

import pytest

from edutoon.domain import visual as v
from tests.visual_corpus import (
    comparison,
    components,
    cycle,
    flow,
    invalid_examples,
    node,
    nodes,
    text,
    tree,
    valid_examples,
)

# --- the corpus --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "spec"), valid_examples(), ids=lambda x: x if isinstance(x, str) else ""
)
def test_every_valid_example_parses(name, spec):
    parsed = v.parse_visual(spec)
    assert parsed.type == spec["type"]


@pytest.mark.parametrize(
    ("name", "category", "value"),
    invalid_examples(),
    ids=[f"{n}" for n, _c, _v in invalid_examples()],
)
def test_every_invalid_example_is_rejected_and_never_repaired(name, category, value):
    before = copy.deepcopy(value)
    with pytest.raises(v.VisualSpecError):
        v.parse_visual(value)
    assert value == before, "the validator must not modify its input"


def test_the_corpus_covers_every_visual_type_and_both_kinds_of_rule():
    assert {spec["type"] for _n, spec in valid_examples()} == set(v.VISUAL_TYPES)
    kinds = {category for _n, category, _v in invalid_examples()}
    assert kinds == {"structural", "semantic"}
    assert len(invalid_examples()) > 150


# --- what a parsed visual looks like -----------------------------------------------------------


def test_a_process_flow_parses_to_its_steps_title_and_focus():
    parsed = v.parse_visual(
        {
            "type": "process_flow",
            "title": "Making sugar",
            "nodes": [{"label": "Light", "detail": "absorbed"}, {"label": "Sugar"}],
            "focus": 1,
        }
    )
    assert isinstance(parsed, v.ProcessFlow)
    assert parsed.title == "Making sugar"
    assert parsed.focus == 1
    assert [n.label for n in parsed.nodes] == ["Light", "Sugar"]
    assert parsed.nodes[0].detail == "absorbed" and parsed.nodes[1].detail is None


def test_a_hierarchy_knows_each_nodes_level():
    parsed = v.parse_visual(tree([None, 0, 0, 1, 2]))
    assert isinstance(parsed, v.Hierarchy)
    assert [parsed.level_of(i) for i in range(5)] == [0, 1, 1, 2, 2]


def test_a_comparison_and_components_parse_to_their_own_shapes():
    cmp = v.parse_visual(comparison(3, 2, "versus"))
    assert isinstance(cmp, v.Comparison) and cmp.relation == "versus" and len(cmp.columns) == 3
    comp = v.parse_visual(components(4, "layers"))
    assert isinstance(comp, v.LabelledComponents) and comp.form == "layers"
    assert len(comp.parts) == 4


def test_text_is_never_trimmed_or_altered():
    parsed = v.parse_visual(flow(2))
    padded = {**flow(2), "nodes": [node(" Light "), node("Sugar")]}
    assert v.parse_visual(padded).nodes[0].label == " Light "  # accepted exactly as written
    assert parsed.nodes[0].label == "aaaaaa"


# --- the limits are exactly the documented numbers ---------------------------------------------


@pytest.mark.parametrize(
    ("build", "low", "high"),
    [
        (flow, v.PROCESS_FLOW_MIN_NODES, v.PROCESS_FLOW_MAX_NODES),
        (cycle, v.CYCLE_MIN_NODES, v.CYCLE_MAX_NODES),
    ],
)
def test_node_counts_are_accepted_exactly_from_min_to_max(build, low, high):
    for count in range(low, high + 1):
        v.parse_visual(build(count))
    for count in (low - 1, high + 1):
        with pytest.raises(v.VisualSpecError):
            v.parse_visual(build(count))


def test_the_documented_limits_are_the_approved_numbers():
    assert (v.PROCESS_FLOW_MIN_NODES, v.PROCESS_FLOW_MAX_NODES) == (2, 6)
    assert (v.CYCLE_MIN_NODES, v.CYCLE_MAX_NODES) == (3, 6)
    assert (v.HIERARCHY_MIN_NODES, v.HIERARCHY_MAX_NODES, v.HIERARCHY_MAX_LEVELS) == (2, 9, 3)
    assert (v.COMPARISON_MIN_COLUMNS, v.COMPARISON_MAX_COLUMNS) == (2, 3)
    assert (v.COMPONENTS_MIN_PARTS, v.COMPONENTS_MAX_PARTS) == (2, 6)
    assert (v.LABEL_MAX_CHARS, v.DETAIL_MAX_CHARS, v.TITLE_MAX_CHARS) == (28, 60, 60)
    assert v.VISUAL_TYPES == (
        "process_flow",
        "cycle",
        "hierarchy",
        "comparison",
        "labelled_components",
    )


def test_a_label_of_exactly_the_maximum_is_accepted_and_one_more_is_not():
    v.parse_visual({"type": "cycle", "nodes": nodes(3)[:2] + [node(text(v.LABEL_MAX_CHARS))]})
    with pytest.raises(v.VisualSpecError, match="at most 28"):
        v.parse_visual({"type": "cycle", "nodes": nodes(3)[:2] + [node(text(29))]})


def test_a_hierarchy_may_be_three_levels_deep_but_not_four():
    v.parse_visual(tree([None, 0, 1]))
    with pytest.raises(v.VisualSpecError, match="levels deep"):
        v.parse_visual(tree([None, 0, 1, 2]))


def test_a_hierarchy_level_holds_at_most_five_nodes():
    v.parse_visual(tree([None, 0, 0, 0, 0, 0]))  # five children of the root
    with pytest.raises(v.VisualSpecError, match="one level"):
        v.parse_visual(tree([None, 0, 0, 0, 0, 0, 0]))


def test_a_parent_must_come_before_its_child_so_a_tree_cannot_contain_a_cycle():
    for shape in ([None, 1, 0], [None, 2, 0], [None, 1, 1]):
        with pytest.raises(v.VisualSpecError, match="parent"):
            v.parse_visual(tree(shape))


def test_there_is_exactly_one_root():
    with pytest.raises(v.VisualSpecError, match="needs a parent"):
        v.parse_visual(tree([None, 0, None]))
    with pytest.raises(v.VisualSpecError, match="root"):
        bad = tree([None, 0])
        bad["nodes"][0]["parent"] = 0
        v.parse_visual(bad)


def test_before_after_needs_exactly_two_columns():
    v.parse_visual(comparison(2, 2, "before_after"))
    with pytest.raises(v.VisualSpecError, match="exactly 2"):
        v.parse_visual(comparison(3, 2, "before_after"))
    v.parse_visual(comparison(3, 2, "versus"))


# --- closed: nothing but the vocabulary --------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        "x",
        "y",
        "width",
        "height",
        "colour",
        "color",
        "font",
        "fontSize",
        "src",
        "url",
        "path",
        "code",
        "eval",
        "style",
    ],
)
def test_no_coordinate_colour_font_path_url_or_code_field_is_ever_accepted(extra):
    for spec in (flow(3), cycle(4), tree([None, 0]), comparison(), components()):
        with pytest.raises(v.VisualSpecError, match="unknown field"):
            v.parse_visual({**spec, extra: "anything"})


def test_unknown_fields_are_rejected_at_every_level_not_just_the_top():
    inside_a_node = flow(3)
    inside_a_node["nodes"][1]["icon"] = "leaf"
    inside_a_column = comparison()
    inside_a_column["columns"][0]["colour"] = "red"
    for spec in (inside_a_node, inside_a_column):
        with pytest.raises(v.VisualSpecError, match="unknown field"):
            v.parse_visual(spec)


def test_a_value_chart_is_not_part_of_the_vocabulary():
    chart = {"type": "value_chart", "values": [1, 2, 3]}
    with pytest.raises(v.VisualSpecError, match="type"):
        v.parse_visual(chart)
    assert "value_chart" not in v.VISUAL_TYPES


@pytest.mark.parametrize("bad", [True, 1.0, "1", None, [1], {"a": 1}])
def test_structural_references_must_be_plain_integers(bad):
    spec = flow(3, focus=bad)
    with pytest.raises(v.VisualSpecError):
        v.parse_visual(spec)


# --- text rules --------------------------------------------------------------------------------


@pytest.mark.parametrize("junk", ["\U0001f600", "\n", "\t", "‮", "​", "\x00", ""])
def test_a_character_no_font_can_draw_rejects_the_visual(junk):
    spec = flow(3)
    spec["nodes"][0]["label"] = f"a{junk}b"
    with pytest.raises(v.VisualSpecError, match="cannot be drawn"):
        v.parse_visual(spec)


def test_symbols_the_renderer_can_draw_are_allowed_in_text():
    spec = flow(3)
    spec["nodes"][0]["label"] = "x ≤ 5 → y ✓"
    spec["nodes"][1]["label"] = "CO₂ + H₂O"
    assert v.parse_visual(spec).nodes[0].label.startswith("x")


def test_the_total_words_of_a_visual_are_capped():
    spec = {"type": "process_flow", "nodes": [node(text(28, "q"), text(60, "w")) for _ in range(6)]}
    with pytest.raises(v.VisualSpecError, match="characters of text"):
        v.parse_visual(spec)


def test_a_spec_at_exactly_the_word_budget_is_accepted():
    # 4 nodes x (28 label + 60 detail) = 352, plus a 48-character title = exactly 400.
    spec = {
        "type": "process_flow",
        "title": text(48, "t"),
        "nodes": [node(text(28, chr(97 + i)), text(60, "d")) for i in range(4)],
    }
    assert v.parse_visual(spec)
    spec["title"] = text(49, "t")
    with pytest.raises(v.VisualSpecError, match="characters of text"):
        v.parse_visual(spec)


def test_the_serialised_size_is_capped():
    spec = {**flow(3), "note": "x" * 3000}
    with pytest.raises(v.VisualSpecError, match="larger than 2048 bytes"):
        v.parse_visual(spec)


@pytest.mark.parametrize("bad", [{1, 2}, object(), float("nan"), float("inf")])
def test_a_value_that_is_not_plain_json_is_rejected(bad):
    with pytest.raises(v.VisualSpecError, match="plain JSON"):
        v.parse_visual({**flow(3), "focus": bad})


def test_error_messages_never_echo_the_offending_text():
    spec = flow(3)
    spec["nodes"][0]["label"] = "SECRET-TEXT " + "x" * 40
    with pytest.raises(v.VisualSpecError) as raised:
        v.parse_visual(spec)
    assert "SECRET-TEXT" not in str(raised.value)


# --- regions -----------------------------------------------------------------------------------


def test_the_illustrated_templates_map_to_a_full_or_half_region():
    assert v.TEMPLATE_REGION == {
        "labelled_diagram": "full",
        "full_bleed_illustration": "full",
        "split_text_illustration": "half",
    }


@pytest.mark.parametrize(
    ("spec", "fits_half"),
    [
        (flow(4), True),
        (flow(5), False),
        (cycle(4), True),
        (cycle(5), False),
        (tree([None, 0, 0, 1, 2]), True),
        (tree([None, 0, 0, 0, 0, 1]), False),
        (comparison(2, 3), True),
        (comparison(2, 4), False),
        (comparison(3, 2), False),
        (components(4), True),
        (components(5), False),
    ],
)
def test_a_visual_too_big_for_the_half_region_is_rejected_for_a_split_scene(spec, fits_half):
    parsed = v.parse_visual(spec)
    assert v.fits_region(parsed, "full") is True
    assert v.fits_region(parsed, "half") is fits_half
    if fits_half:
        v.validate_visual_for_template("split_text_illustration", parsed)
    else:
        with pytest.raises(v.VisualSpecError, match="too large"):
            v.validate_visual_for_template("split_text_illustration", parsed)
    v.validate_visual_for_template("full_bleed_illustration", parsed)
    v.validate_visual_for_template("labelled_diagram", parsed)


@pytest.mark.parametrize("template", ["title_card", "bullet_points", "formula_or_code"])
def test_a_text_only_template_has_no_place_for_a_visual(template):
    with pytest.raises(v.VisualSpecError, match="no place"):
        v.validate_visual_for_template(template, v.parse_visual(flow(3)))


# --- reading and dropping a visual from scene content ------------------------------------------


def _content(visual, **illustration):
    return {"illustration": {"description": "A diagram", "visual": visual, **illustration}}


def test_visual_of_returns_the_parsed_visual_or_none_and_never_raises():
    assert isinstance(v.visual_of(_content(flow(3))), v.ProcessFlow)
    assert v.visual_of({"illustration": {"description": "d"}}) is None
    assert v.visual_of(_content({"type": "nonsense"})) is None
    assert v.visual_of({"title": "no illustration"}) is None
    assert v.visual_of({"illustration": "not a mapping"}) is None


def test_a_valid_visual_is_kept_unchanged():
    content = _content(flow(3))
    result, dropped = v.without_invalid_visual("full_bleed_illustration", content)
    assert dropped is False
    assert result == content


def test_an_invalid_visual_is_dropped_the_scene_is_kept_and_the_drop_is_marked():
    content = {**_content({"type": "cycle", "nodes": nodes(2)}), "caption": "Keep me"}
    result, dropped = v.without_invalid_visual("full_bleed_illustration", content)
    assert dropped is True
    assert "visual" not in result["illustration"]
    assert result["illustration"]["visual_dropped"] is True
    assert result["illustration"]["description"] == "A diagram"
    assert result["caption"] == "Keep me"
    assert "visual" in content["illustration"], "the input must not be modified"


def test_a_visual_too_big_for_its_template_is_dropped_too():
    result, dropped = v.without_invalid_visual("split_text_illustration", _content(flow(6)))
    assert dropped is True
    assert result["illustration"]["visual_dropped"] is True
    kept, was = v.without_invalid_visual("full_bleed_illustration", _content(flow(6)))
    assert was is False and "visual" in kept["illustration"]


def test_a_null_visual_is_a_dropped_visual():
    result, dropped = v.without_invalid_visual("full_bleed_illustration", _content(None))
    assert dropped is True
    assert "visual" not in result["illustration"]


def test_a_scene_with_no_visual_is_untouched():
    content = {"illustration": {"description": "d", "subject_labels": ["a"]}}
    result, dropped = v.without_invalid_visual("full_bleed_illustration", content)
    assert (result, dropped) == (content, False)


def test_a_drop_flag_written_by_the_model_is_discarded_only_the_service_sets_it():
    with_flag = {"illustration": {"description": "d", "visual_dropped": True}}
    result, dropped = v.without_invalid_visual("full_bleed_illustration", with_flag)
    assert dropped is False
    assert "visual_dropped" not in result["illustration"]
    valid_with_flag = _content(flow(3), visual_dropped=True)
    result, dropped = v.without_invalid_visual("full_bleed_illustration", valid_with_flag)
    assert dropped is False
    assert "visual_dropped" not in result["illustration"]
    assert "visual" in result["illustration"]


def test_a_scene_without_an_illustration_mapping_is_returned_as_is():
    result, dropped = v.without_invalid_visual("full_bleed_illustration", {"caption": "c"})
    assert (result, dropped) == ({"caption": "c"}, False)


def test_visual_problem_explains_a_rejection_without_echoing_the_text():
    bad = _content({"type": "cycle", "nodes": [node("SECRET-LABEL")]})
    reason = v.visual_problem("full_bleed_illustration", bad)
    assert reason is not None and "nodes" in reason
    assert "SECRET-LABEL" not in reason


def test_visual_problem_is_none_for_no_visual_a_valid_one_and_a_scene_with_no_illustration():
    assert (
        v.visual_problem("full_bleed_illustration", {"illustration": {"description": "d"}}) is None
    )
    assert v.visual_problem("full_bleed_illustration", _content(flow(3))) is None
    assert v.visual_problem("full_bleed_illustration", {"caption": "c"}) is None


def test_visual_problem_names_a_visual_too_big_for_its_template():
    reason = v.visual_problem("split_text_illustration", _content(flow(6)))
    assert reason is not None and "too large" in reason
