"""``domain.storyboard_provenance`` - the hard numeric/proper-noun provenance
rule and the soft lexical-overlap reviewer signal (Phase 9).

No I/O, no provider, no database. Mutation target: the numeric/proper-noun
provenance rule itself (spec section 25, item 6) - a mutation that let a
fabricated numeric claim through must be caught.
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
    "Photosynthesis converts sunlight into chemical energy inside the chloroplast. "
    "The process was first described in detail by Jan Ingenhousz in 1779, and it "
    "typically happens at around 25 degrees Celsius."
)


# --- honest content is accepted --------------------------------------------------


def test_a_faithful_paraphrase_has_no_violations() -> None:
    content = {
        "title": "Photosynthesis",
        "bullets": [
            "Sunlight becomes chemical energy",
            "Happens inside the chloroplast",
            "Described by Jan Ingenhousz in 1779",
        ],
    }
    assert check_provenance(content, NARRATION) == []
    require_provenance(content, NARRATION)  # does not raise


def test_reusing_a_number_from_narration_is_accepted() -> None:
    content = {"content": "It happens at around 25 degrees Celsius.", "kind": "formula"}
    assert check_provenance(content, NARRATION) == []


def test_text_with_no_protected_tokens_is_accepted() -> None:
    content = {"title": "A gentle introduction to plants"}
    assert check_provenance(content, NARRATION) == []


def test_empty_content_is_accepted() -> None:
    assert check_provenance({}, NARRATION) == []


# --- fabricated number / percentage / date / unit are rejected -----------------------


def test_a_fabricated_number_is_rejected() -> None:
    violations = check_provenance({"title": "It takes 42 minutes"}, NARRATION)
    assert any(v.kind == "number" and v.token == "42" for v in violations)


def test_a_fabricated_percentage_is_rejected() -> None:
    violations = check_provenance({"caption": "Efficiency reaches 97%"}, NARRATION)
    assert any(v.kind == "number" and v.token == "97" for v in violations)


def test_a_fabricated_date_year_is_rejected() -> None:
    violations = check_provenance({"subtitle": "Discovered in 1901"}, NARRATION)
    assert any(v.kind == "number" and v.token == "1901" for v in violations)


def test_a_fabricated_month_name_is_rejected() -> None:
    violations = check_provenance({"subtitle": "First observed in March"}, NARRATION)
    assert any(v.kind == "date_word" and v.token == "march" for v in violations)


def test_a_fabricated_unit_bearing_quantity_is_rejected() -> None:
    violations = check_provenance({"text": "The leaf is 12 centimetres wide"}, NARRATION)
    assert any(v.kind == "unit" and v.token == "centimetres" for v in violations)


def test_a_fabricated_currency_amount_is_rejected() -> None:
    violations = check_provenance({"caption": "Costs $500 to study"}, NARRATION)
    assert any(v.kind == "number" and v.token == "500" for v in violations)
    assert any(v.kind == "currency" and v.token == "$" for v in violations)


def test_a_fabricated_number_word_is_rejected() -> None:
    violations = check_provenance({"title": "Twelve stages of the process"}, NARRATION)
    assert any(v.kind == "number_word" and v.token == "twelve" for v in violations)


# --- fabricated proper nouns rejected where detectable --------------------------------


def test_a_fabricated_proper_noun_is_rejected() -> None:
    violations = check_provenance({"subtitle": "As explained by Charles Darwin"}, NARRATION)
    tokens = {v.token for v in violations if v.kind == "proper_noun"}
    assert "Darwin" in tokens
    assert "Charles" in tokens


def test_a_narration_supported_proper_noun_is_accepted_case_insensitively() -> None:
    content = {"subtitle": "A note from jan ingenhousz"}
    violations = check_provenance(content, NARRATION)
    assert not any(v.kind == "proper_noun" for v in violations)


def test_a_capitalised_word_at_the_start_of_a_sentence_is_not_flagged() -> None:
    """Ordinary sentence-initial capitalisation is not treated as a proper
    noun (documented limitation) - it would otherwise flag almost every
    sentence.
    """
    content = {"text": "Sunlight drives the whole process forward."}
    violations = check_provenance(content, NARRATION)
    assert not any(v.kind == "proper_noun" for v in violations)


def test_an_all_caps_acronym_is_not_flagged_as_a_proper_noun() -> None:
    """Documented limitation: an ALL-CAPS acronym is excluded from the
    proper-noun heuristic (more often a legitimate domain term than a
    fabrication).
    """
    content = {"label": "See the DNA diagram", "content": "x", "kind": "code"}
    violations = check_provenance(content, NARRATION)
    assert not any(v.kind == "proper_noun" for v in violations)


# --- require_provenance raises with every violation ------------------------------------


def test_require_provenance_raises_for_a_violation() -> None:
    with pytest.raises(FabricatedContentError) as excinfo:
        require_provenance({"title": "Invented in 1850"}, NARRATION)
    assert len(excinfo.value.violations) >= 1


def test_require_provenance_does_not_raise_for_honest_content() -> None:
    require_provenance({"title": "Photosynthesis"}, NARRATION)


def test_require_provenance_reports_every_violation_not_just_the_first() -> None:
    with pytest.raises(FabricatedContentError) as excinfo:
        require_provenance(
            {"title": "42 facts from Napoleon in 1850"}, NARRATION
        )
    assert len(excinfo.value.violations) >= 3


# --- violations reach into nested slots (labels, illustration, bullets) --------------


def test_a_violation_inside_an_illustration_description_is_caught() -> None:
    content = {"illustration": {"description": "A chart showing 88 percent efficiency"}}
    violations = check_provenance(content, NARRATION)
    assert any(v.slot_path == "illustration.description" for v in violations)


def test_a_violation_inside_a_label_list_item_is_caught() -> None:
    content = {
        "illustration": {"description": "A leaf"},
        "labels": [{"text": "Grown in 1999", "position": "top"}],
    }
    violations = check_provenance(content, NARRATION)
    assert any(v.slot_path == "labels[0].text" for v in violations)


def test_a_violation_inside_a_bullet_list_item_is_caught() -> None:
    content = {"bullets": ["Takes exactly 30 seconds", "and more"]}
    violations = check_provenance(content, NARRATION)
    assert any(v.slot_path == "bullets[0]" for v in violations)


# --- soft lexical-overlap signal ---------------------------------------------------


def test_overlapping_text_has_overlap() -> None:
    content = {"title": "Photosynthesis and chlorophyll"}
    assert has_lexical_overlap(content, NARRATION) is True


def test_unrelated_text_has_no_overlap() -> None:
    content = {"title": "Volcanic eruptions reshape coastlines"}
    assert has_lexical_overlap(content, NARRATION) is False


def test_no_overlap_never_raises_only_a_signal() -> None:
    # A False return is the whole contract - callers decide what to do with it;
    # this function itself must never reject.
    assert has_lexical_overlap({"title": "Completely unrelated topic here"}, NARRATION) is False


def test_empty_content_counts_as_having_overlap() -> None:
    assert has_lexical_overlap({}, NARRATION) is True
