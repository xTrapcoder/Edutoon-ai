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


def test_a_proper_noun_that_is_a_plural_of_a_narration_word_is_accepted() -> None:
    """``Chloroplasts`` in a subtitle is the narration's ``chloroplast``: a real local-model run
    failed a whole segment over exactly this. A different name is still rejected."""
    narration = "Photosynthesis happens in a part of the cell called the chloroplast."

    assert not check_provenance({"subtitle": "Chloroplasts"}, narration)
    assert not check_provenance({"subtitle": "Where it happens: Chloroplasts"}, narration)
    flagged = {v.token for v in check_provenance({"subtitle": "In Mitochondria"}, narration)}
    assert flagged == {"Mitochondria"}


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
        require_provenance({"title": "42 facts from Napoleon in 1850"}, NARRATION)
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


# --- text on a slide that the narration never says ----------------------------------------------
#
# Regression, from a fresh scanned-PDF run: the storyboard model put "This pattern is known as
# semidiurnal tides." on a slide whose narration said only "Most coasts experience two high tides
# and two low tides each day", and the subtitle "The dark side" under "At new moon". Neither is a
# number, date or name, so the hard provenance rule could not see them.

from edutoon.domain.storyboard_provenance import without_unnamed_scene_text  # noqa: E402

TIDES_NARRATION = (
    "Tides are the regular rise and fall of the sea. The Moon's gravity mainly causes tides. "
    "Most coasts experience two high tides and two low tides each day."
)


def test_a_bullet_the_narration_never_says_is_removed_and_the_rest_stay():
    content = {
        "bullets": [
            "Most coasts experience two high tides and two low tides each day.",
            "This pattern is known as semidiurnal tides.",
            "The Moon's gravity mainly causes tides",
        ]
    }

    trimmed, removed = without_unnamed_scene_text("bullet_points", content, TIDES_NARRATION)

    assert removed == ["This pattern is known as semidiurnal tides."]
    assert trimmed == {
        "bullets": [
            "Most coasts experience two high tides and two low tides each day.",
            "The Moon's gravity mainly causes tides",
        ]
    }


def test_a_bullet_slide_left_with_fewer_than_two_bullets_gives_none():
    content = {
        "bullets": [
            "This pattern is known as semidiurnal tides.",
            "Most coasts experience two high tides and two low tides each day.",
        ]
    }

    trimmed, removed = without_unnamed_scene_text("bullet_points", content, TIDES_NARRATION)

    assert trimmed is None and len(removed) == 1


def test_an_invented_subtitle_is_removed_and_the_title_stays():
    narration = "At new moon, the side facing Earth is not lit, making it look dark."

    trimmed, removed = without_unnamed_scene_text(
        "title_card",
        {"title": "At new moon", "subtitle": "The dark side of lunar phases"},
        narration,
    )

    assert trimmed == {"title": "At new moon"} and removed == ["The dark side of lunar phases"]


def test_a_subtitle_made_of_the_narrations_words_stays():
    narration = "At new moon, the side facing Earth is not lit, making it look dark."

    trimmed, removed = without_unnamed_scene_text(
        "title_card",
        {"title": "At new moon", "subtitle": "The side facing Earth is not lit"},
        narration,
    )

    assert removed == [] and trimmed is not None and trimmed["subtitle"].startswith("The side")


def test_a_paraphrase_with_one_stray_word_is_kept():
    narration = "Suspension bridges use vertical cables to hang the deck from towers."

    trimmed, removed = without_unnamed_scene_text(
        "bullet_points",
        {"bullets": ["Cables hang the road deck from towers", "Vertical cables support the deck"]},
        narration,
    )

    assert removed == [] and trimmed is not None and len(trimmed["bullets"]) == 2


def test_other_templates_are_left_alone():
    content = {"title": "Anything", "body": "Whatever the model wrote"}

    assert without_unnamed_scene_text("formula_or_code", content, "x") == (content, [])
