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


# --- is the claim's wording in the passages it cites? --------------------------------------------
#
# Regression, from fresh ecosystems runs: the judge approved "Humans alter ecosystems through land
# clearing, which involves removing vegetation to make way for agriculture or development" citing a
# page about energy flow, and "Animals that eat plants are called herbivores" citing the same page
# although another passage states it.

from edutoon.domain.claim_grounding import (  # noqa: E402
    CITATION_COVERAGE_FLOOR,
    best_covering_passage,
    claim_coverage,
    thin_citation_rationale,
)

ENERGY_PAGE = (
    "Energy does not recycle through an ecosystem. It flows in one direction, from the sun to "
    "producers, then to consumers, and is finally lost as heat. At each trophic level only about "
    "10 percent of the energy is stored in the bodies of the organisms at the next level."
)
CONSUMERS_PAGE = (
    "Animals cannot make their own food, so they are called consumers. Herbivores eat plants and "
    "are primary consumers. Carnivores that eat herbivores are secondary consumers."
)
HUMANS_PAGE = (
    "Humans change ecosystems by clearing land, polluting water and introducing species from "
    "other places. An introduced species that spreads quickly and harms native species is called "
    "an invasive species."
)


def test_a_claim_is_covered_by_the_passage_that_states_it():
    claim = "Herbivores eat plants and are primary consumers."

    assert claim_coverage(claim, [CONSUMERS_PAGE]) == 1.0
    assert claim_coverage(claim, [ENERGY_PAGE]) < CITATION_COVERAGE_FLOOR


def test_a_claim_dressed_in_words_the_source_never_uses_is_thinly_covered():
    claim = (
        "Humans alter ecosystems through land clearing, which involves removing vegetation to "
        "make way for agriculture or development."
    )

    assert claim_coverage(claim, [HUMANS_PAGE]) < 0.6
    assert claim_coverage(claim, [ENERGY_PAGE]) < CITATION_COVERAGE_FLOOR


def test_a_plural_or_other_form_of_a_word_counts_as_covered():
    assert claim_coverage("Invasive species spread quickly.", [HUMANS_PAGE]) >= 0.75


def test_the_passage_that_states_a_claim_is_found_among_the_retrieved_ones():
    claim = "Animals that eat plants are called herbivores."

    assert best_covering_passage(claim, [ENERGY_PAGE, CONSUMERS_PAGE, HUMANS_PAGE]) == 1


def test_no_passage_is_named_when_none_states_the_claim_well_enough():
    claim = "Fungi and bacteria are tiny living things that we cannot see with our eyes."

    assert best_covering_passage(claim, [ENERGY_PAGE, CONSUMERS_PAGE, HUMANS_PAGE]) is None


def test_a_claim_with_no_content_words_is_not_judged_by_coverage():
    assert claim_coverage("It is.", [ENERGY_PAGE]) == 1.0


def test_the_thin_citation_rationale_keeps_the_judges_words_and_says_how_much_was_found():
    text = thin_citation_rationale("Passage [p1] supports it.", 0.31)

    assert text.startswith("Passage [p1] supports it.")
    assert "31%" in text and "partly supported" in text
