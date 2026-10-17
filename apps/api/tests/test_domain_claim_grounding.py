"""``domain/claim_grounding.py`` - pure, no fixtures, no DB.

Regression: a claim stored as *supported* said "Factory towns in India grew rapidly, with the
population of cities like Manchester, Birmingham, and Leeds increasing sharply" although the
passage never mentioned India.
"""

from __future__ import annotations

from edutoon.domain.claim_grounding import downgrade_rationale, ungrounded_entities

PASSAGE = (
    "Factory towns grew very fast. Manchester, Birmingham and Leeds filled with workers, and the "
    "population of cities rose sharply. In 1833 the Factory Act limited the working hours of "
    "children."
)


def test_an_invented_place_is_found():
    claim = (
        "Factory towns in India grew rapidly, with the population of cities like Manchester, "
        "Birmingham, and Leeds increasing sharply."
    )
    assert ungrounded_entities(claim, [PASSAGE]) == ("India",)


def test_a_claim_that_only_rewords_the_passage_is_grounded():
    claim = "The population of Manchester, Birmingham and Leeds rose sharply."
    assert ungrounded_entities(claim, [PASSAGE]) == ()


def test_an_invented_figure_or_date_is_found():
    assert ungrounded_entities("The Factory Act of 1847 limited children's hours.", [PASSAGE]) == (
        "1847",
    )


def test_a_figure_the_passage_has_is_grounded():
    assert ungrounded_entities("The Factory Act of 1833 limited children's hours.", [PASSAGE]) == ()


def test_the_name_may_be_in_any_one_of_the_passages():
    claim = "Leeds grew quickly, and Yorkshire mills drove it."
    assert ungrounded_entities(claim, [PASSAGE, "Yorkshire mills were large."]) == ()


def test_a_sentence_initial_capital_is_not_a_name():
    assert ungrounded_entities("Workers moved to towns.", ["Some people moved."]) == ()


def test_an_ordinal_or_a_formula_coefficient_is_not_a_new_figure():
    assert (
        ungrounded_entities("It began in the late 18th century.", ["the eighteenth century"]) == ()
    )
    assert (
        ungrounded_entities("Plants make 6CO2 into sugar.", ["Carbon dioxide becomes sugar."]) == ()
    )


def test_a_name_that_the_passage_writes_in_another_form_is_grounded():
    assert (
        ungrounded_entities("Some volcanoes, like those in Hawaii, form.", ["the Hawaiian Islands"])
        == ()
    )


def test_a_short_name_must_appear_as_itself():
    assert ungrounded_entities("Trade with Peru grew.", ["Perusal of the papers continued."]) == (
        "Peru",
    )


def test_a_year_is_still_checked_even_beside_an_ordinal():
    assert ungrounded_entities(
        "In 1847 the 18th amendment passed.", ["the eighteenth amendment"]
    ) == ("1847",)


def test_without_passages_nothing_is_rejected():
    assert ungrounded_entities("Factory towns in India grew.", []) == ()


def test_each_missing_name_is_listed_once_in_order():
    claim = "Trade between India and China and India again grew."
    assert ungrounded_entities(claim, ["Trade grew."]) == ("India", "China")


def test_the_rationale_keeps_the_judges_words_and_says_what_was_found():
    text = downgrade_rationale("Passage [p1] supports it.", ("India",))
    assert text.startswith("Passage [p1] supports it.")
    assert "'India'" in text
    assert "partly supported" in text


def test_the_rationale_stands_alone_without_a_judge_rationale():
    assert downgrade_rationale(None, ("India",)).startswith("The claim mentions 'India'")
