"""Provenance for procedural visuals and numeric JSON leaves (Phase 11).

The hard rule is unchanged - no number, date, percentage, unit-bearing quantity or capitalised
proper noun in scene text unless the segment's own narration also has it - and it now covers what
a visual adds: nested visual text, numeric JSON leaves (a number smuggled in as data rather than
text), and structural integer indices, which are exempt because they point at the visual's own
items and carry no claim.
"""

from __future__ import annotations

import pytest

from edutoon.domain.storyboard_provenance import (
    FabricatedContentError,
    check_provenance,
    has_lexical_overlap,
    require_provenance,
)

NARRATION = (
    "Water evaporates from the ocean and condenses into clouds. About 71 percent of the Earth "
    "is covered by water, and rain falls in Mumbai during June. A raindrop may fall for 3.50 "
    "minutes."
)


def _flow(*labels: str, **extra) -> dict:
    return {
        "illustration": {
            "description": "The water cycle",
            "visual": {
                "type": "process_flow",
                "nodes": [{"label": text} for text in labels],
                **extra,
            },
        }
    }


def _violations(content: dict, narration: str = NARRATION) -> list[tuple[str, str]]:
    return [(v.kind, v.token) for v in check_provenance(content, narration)]


# --- honest visual text is accepted ---------------------------------------------------------


def test_an_honest_paraphrase_in_a_visual_has_no_violation():
    content = _flow("Water evaporates", "Clouds form", "Rain falls")
    assert check_provenance(content, NARRATION) == []
    require_provenance(content, NARRATION)  # does not raise


def test_numbers_dates_and_names_the_narration_has_are_allowed_in_a_visual():
    content = _flow("71 percent water", "Rain in Mumbai", "Falls during June")
    assert check_provenance(content, NARRATION) == []


# --- fabrication in visual text is a hard failure -----------------------------------------------


def test_a_fabricated_percentage_in_a_visual_label_is_a_violation():
    assert ("number", "40") in _violations(_flow("40% of rain evaporates", "Clouds form"))


def test_a_fabricated_number_in_a_visual_detail_is_a_violation():
    content = {
        "illustration": {
            "description": "d",
            "visual": {
                "type": "cycle",
                "nodes": [
                    {"label": "Rain"},
                    {"label": "Sea", "detail": "12 steps"},
                    {"label": "Sky"},
                ],
            },
        }
    }
    assert ("number", "12") in _violations(content)


def test_a_fabricated_unit_in_a_visual_is_a_violation():
    kinds = _violations(_flow("Travels 5 kilometres", "Clouds form"))
    assert ("unit", "kilometres") in kinds
    assert ("number", "5") in kinds


def test_a_fabricated_date_in_a_visual_is_a_violation():
    assert ("date_word", "march") in _violations(_flow("Rain in March", "Clouds form"))


def test_a_fabricated_proper_noun_in_a_visual_is_a_violation():
    assert ("proper_noun", "Delhi") in _violations(_flow("Rain over Delhi", "Clouds form"))


def test_a_violation_deep_inside_a_visual_names_the_slot_it_was_found_in():
    [violation] = check_provenance(_flow("Water evaporates", "Rain falls in 1999"), NARRATION)
    assert violation.slot_path == "illustration.visual.nodes[1].label"
    assert violation.token == "1999"


def test_require_provenance_reports_every_violation_not_just_the_first():
    content = _flow("40% gone", "Rain over Delhi")
    with pytest.raises(FabricatedContentError) as raised:
        require_provenance(content, NARRATION)
    assert {v.token for v in raised.value.violations} >= {"40", "Delhi"}


def test_comparison_columns_and_component_parts_are_checked_too():
    comparison = {
        "illustration": {
            "description": "d",
            "visual": {
                "type": "comparison",
                "relation": "versus",
                "columns": [
                    {"heading": "Sea", "points": ["Salty", "Covers 99 percent"]},
                    {"heading": "Land", "points": ["Dry", "Has Rivers"]},
                ],
            },
        }
    }
    tokens = {token for _kind, token in _violations(comparison)}
    assert "99" in tokens
    components = {
        "illustration": {
            "description": "d",
            "visual": {
                "type": "labelled_components",
                "subject": "The Sahara",
                "form": "hub",
                "parts": [{"label": "Sand"}, {"label": "Rain"}],
            },
        }
    }
    assert ("proper_noun", "Sahara") in _violations(components)


def test_text_in_a_visual_that_is_about_to_be_dropped_is_still_checked():
    """An invalid visual (one node) is dropped later - but fabrication in it is never discarded
    quietly: the whole scene set fails."""
    content = {
        "illustration": {
            "description": "d",
            "visual": {"type": "cycle", "nodes": [{"label": "Around 99 percent"}]},
        }
    }
    assert ("number", "99") in _violations(content)


# --- numeric JSON leaves cannot bypass the rule ------------------------------------------------


def test_a_fabricated_number_stored_as_a_json_number_is_a_violation():
    assert ("number", "73") in _violations({"title": "Water", "stat": 73})


def test_a_json_number_the_narration_has_is_allowed():
    assert _violations({"title": "Water", "stat": 71}) == []


def test_an_integral_float_is_the_same_number_as_the_integer():
    assert _violations({"stat": 71.0}) == []
    assert ("number", "71.5") in _violations({"stat": 71.5})


def test_numbers_that_differ_only_in_trailing_zeros_are_the_same_number():
    assert _violations({"stat": 3.5}) == []  # the narration says 3.50
    assert _violations({"title": "About 3.5 minutes"}) == []
    assert _violations({"title": "About 3.50 minutes"}) == []


def test_a_negative_number_is_checked_by_its_magnitude():
    assert _violations({"stat": -71}) == []
    assert ("number", "40") in _violations({"stat": -40})


def test_numeric_leaves_in_lists_and_nested_objects_are_checked():
    assert ("number", "88") in _violations({"values": [71, 88]})
    assert ("number", "9") in _violations({"a": {"b": [{"c": 9}]}})
    assert ("number", "6") in _violations({"matrix": [[71, 6]]})


def test_a_visual_may_not_carry_a_numeric_value_field_either():
    content = _flow("Water", "Rain")
    content["illustration"]["visual"]["value"] = 42
    assert ("number", "42") in _violations(content)


def test_booleans_and_nulls_are_not_numbers():
    assert _violations({"flag": True, "nothing": None, "more": False}) == []


def test_non_finite_numbers_are_violations():
    assert _violations({"x": float("nan")}) != []
    assert _violations({"x": float("inf")}) != []


def test_a_currency_amount_as_text_still_needs_the_symbol_and_the_number_in_the_narration():
    assert ("currency", "₹") in _violations({"title": "Costs ₹500"})
    assert ("number", "500") in _violations({"title": "Costs ₹500"})


# --- structural integer indices are exempt -----------------------------------------------------


def test_structural_indices_inside_a_visual_are_not_numbers_to_verify():
    content = {
        "illustration": {
            "description": "d",
            "visual": {
                "type": "hierarchy",
                "focus": 2,
                "nodes": [
                    {"label": "Water"},
                    {"label": "Ocean", "parent": 0},
                    {"label": "Cloud", "parent": 0},
                    {"label": "Rain", "parent": 2},
                ],
            },
        }
    }
    # The narration has no 2, 0 or 3 as a digit run other than "71" and "3.50" - indices pass.
    assert check_provenance(content, "Water, oceans, clouds and rain.") == []


def test_the_exemption_covers_only_integer_indices_inside_a_visual():
    outside = {"focus": 2, "parent": 4}
    assert {token for _k, token in _violations(outside, "no digits here")} == {"2", "4"}
    inside = {"illustration": {"visual": {"focus": 2.5}}}
    assert ("number", "2.5") in _violations(inside, "no digits here")


# --- enumerated fields hold structure, not claims ----------------------------------------------


def test_a_visuals_enumerated_fields_are_not_scanned_as_text():
    content = {
        "illustration": {
            "description": "d",
            "visual": {
                "type": "comparison",
                "relation": "before_after",
                "columns": [
                    {"heading": "Old", "points": ["Water", "Rain"]},
                    {"heading": "New", "points": ["Clouds", "Rain"]},
                ],
            },
        }
    }
    assert check_provenance(content, "Old and new water, clouds and rain.") == []


def test_an_enum_field_holding_anything_but_an_allowed_value_is_scanned():
    """Junk in an enum field cannot hide text: only the allowed values are skipped."""
    content = {"illustration": {"visual": {"type": "second", "form": "March 40"}}}
    tokens = {token for _kind, token in _violations(content, "plain narration")}
    assert {"second", "march", "40"} <= tokens


def test_enumerated_field_values_are_not_counted_as_overlap_with_the_narration():
    content = {
        "illustration": {
            "description": "zzzz",
            "visual": {"type": "process_flow", "nodes": [{"label": "qqqq"}, {"label": "wwww"}]},
        }
    }
    assert has_lexical_overlap(content, "process flow narration about process and flow") is False


def test_lexical_overlap_still_sees_visual_words():
    content = _flow("Evaporation", "Condensation")
    assert has_lexical_overlap(content, "Water evaporates; evaporation feeds clouds.") is True
