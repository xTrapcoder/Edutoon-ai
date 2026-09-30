"""The script grounding guard: a segment may not bring in a figure or a name that its verified
claims do not contain, and ``connective`` is a bounded claim, not a free pass.

Two halves. The pure rules (``domain/script_provenance.py``) are exercised on narration written the
way a teacher speaks, both what must pass - ordinary explanation, analogies, "first", "one", "day",
sentence-initial capitals, possessives - and what must not: an invented number, a date, a place, a
person. Then the service: a generation whose section adds one is rejected as a whole, with a message
for the owner and nothing attached, while a normal one is untouched. The guard never judges whether
a sentence is true, only that it does not introduce a figure or a name from nowhere.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.domain.script import ScriptDraft, ScriptSegmentDraft
from edutoon.domain.script_provenance import (
    MAX_CONNECTIVE_WORDS,
    GroundingReference,
    UngroundedSegmentError,
    UnsourcedSegmentError,
    filter_and_validate,
)

WPM = 150
TOPIC = uuid4()
OTHER_TOPIC = uuid4()
CLAIM = uuid4()

# What the writer was shown for the topic (its title + its verified claims), and the lesson frame.
PHOTOSYNTHESIS = (
    "Photosynthesis "
    "Plants use sunlight, water and carbon dioxide to make glucose and oxygen. "
    "Chlorophyll in the chloroplasts of a leaf absorbs light. "
    "Jan Ingenhousz showed in 1779 that plants need light to release oxygen. "
    "The process takes place mainly in the leaves of Earth's green plants."
)
GROUNDING = GroundingReference(
    topic_texts={TOPIC: PHOTOSYNTHESIS, OTHER_TOPIC: "Respiration Cells release energy."},
    frame_text="Biology basics Photosynthesis Respiration",
)


def segment(
    text: str, *, cite: bool = True, connective: bool = False, topic: object = TOPIC
) -> ScriptSegmentDraft:
    return ScriptSegmentDraft(
        segment_index=0,
        narration_text=text,
        claim_ids=[str(CLAIM)] if cite else [],
        estimated_duration_ms=0,
        connective=connective,
        topic_id=topic,  # type: ignore[arg-type]
    )


def check(*segments: ScriptSegmentDraft, grounding: GroundingReference | None = GROUNDING):
    draft = ScriptDraft(title="T", segments=list(segments), estimated_duration_ms=0)
    return filter_and_validate(draft, {CLAIM}, WPM, grounding=grounding)


def unsupported(text: str, **kwargs: object) -> list[str]:
    with pytest.raises(UngroundedSegmentError) as caught:
        check(segment(text, **kwargs))  # type: ignore[arg-type]
    return [token for _, token in caught.value.tokens]


# --- what teaching sounds like must pass ----------------------------------------------------

EXPLANATORY = [
    "Imagine a leaf as a tiny kitchen, where sunlight is the energy that cooks the meal.",
    "First, the plant takes in water through its roots. Second, it takes in carbon dioxide.",
    "One of the most important things to remember is that light drives the whole process.",
    "This happens every day, and it has happened for a very long time.",
    "Think of it like charging a battery: the light goes in and stored energy comes out.",
    "So, in short, plants make their own food. That is why we call them producers.",
    "Chlorophyll, the green pigment, is what absorbs the light. Without it, nothing happens.",
    "Earth's plants do this all year round; the leaf is where most of it takes place.",
    "The DNA of the plant is not involved in this step, and neither is the CO2 in the soil.",
    "Now that we understand light, let's look at how the glucose is used. Ready? Let us begin.",
]


@pytest.mark.parametrize("text", EXPLANATORY)
def test_ordinary_explanatory_wording_passes(text: str) -> None:
    [validated] = check(segment(text)).segments
    assert validated.claim_verification_ids == (CLAIM,)


def test_a_figure_that_a_claim_contains_is_fine_however_it_is_written() -> None:
    check(segment("Jan Ingenhousz found this out in 1779, long before we knew about chloroplasts."))
    check(segment("It was Ingenhousz who showed it, back in 1779."))


def test_a_name_from_any_claim_the_writer_was_shown_for_the_topic_is_fine() -> None:
    """Not only from the claim a segment cites: a fact the topic's verified claims contain is
    supported wherever in the section it is used."""
    check(segment("Ingenhousz did this. The chloroplasts do that.", cite=True))


def test_the_projects_and_topics_own_titles_may_be_named() -> None:
    check(segment("In these Biology basics, we begin with Photosynthesis and then Respiration."))


def test_a_possessive_is_the_same_name() -> None:
    check(segment("The leaf is where most of Earth's food is made."))


# --- what must not pass ---------------------------------------------------------------------


def test_an_invented_number_is_rejected_and_named() -> None:
    assert unsupported("A leaf makes 42 grams of sugar in an hour.") == ["42"]


def test_an_invented_year_or_month_is_rejected() -> None:
    assert "1801" in unsupported("It was shown again in 1801 by another scientist.")
    assert "March" in unsupported("Every year in March the new leaves appear.")


def test_an_invented_place_or_person_is_rejected() -> None:
    assert unsupported("Picture the Amazon rainforest, where this happens on a huge scale.") == [
        "Amazon"
    ]
    assert unsupported("As Darwin observed, plants bend towards the light.") == ["Darwin"]


def test_an_invented_currency_amount_is_rejected() -> None:
    assert "$" in unsupported("A greenhouse like this can cost $200 to build.")


def test_a_percentage_is_a_number_and_must_be_in_the_claims() -> None:
    assert unsupported("Roughly 21% of the air is oxygen, which is why we can breathe.") == ["21"]


def test_a_fact_from_a_different_topics_claims_does_not_ground_this_topic() -> None:
    other = GroundingReference(
        topic_texts={TOPIC: "Photosynthesis Plants make food.", OTHER_TOPIC: "Mitochondria"},
        frame_text="",
    )
    with pytest.raises(UngroundedSegmentError):
        check(segment("The Mitochondria are involved too."), grounding=other)


def test_a_segment_with_no_topic_is_held_to_the_lesson_frame_alone() -> None:
    with pytest.raises(UngroundedSegmentError):
        check(segment("Ingenhousz in 1779.", topic=None))
    check(segment("We begin with Photosynthesis.", topic=None))


def test_only_the_first_offending_segment_is_reported_and_it_is_named_by_position() -> None:
    draft = ScriptDraft(
        title="T",
        segments=[
            segment("Plants make food from light."),
            segment("Then 9000 things happen."),
        ],
        estimated_duration_ms=0,
    )
    with pytest.raises(UngroundedSegmentError) as caught:
        filter_and_validate(draft, {CLAIM}, WPM, grounding=GROUNDING)
    assert caught.value.segment_index == 1
    assert "segment 2" in str(caught.value) and "'9000'" in str(caught.value)


def test_the_message_is_written_for_the_owner_and_lists_at_most_five_tokens() -> None:
    error = UngroundedSegmentError(0, [("number", str(n)) for n in range(20, 30)])
    assert str(error).count("'") == 10  # five tokens, two quotes each
    assert "nothing unsupported may reach the script" in str(error)


# --- connective is a bounded claim ----------------------------------------------------------


def test_a_short_hand_off_with_no_citation_is_still_allowed() -> None:
    [validated] = check(
        segment(
            "Now that we have seen how plants feed, let us look at respiration.",
            cite=False,
            connective=True,
        )
    ).segments
    assert validated.connective is True and validated.claim_verification_ids == ()


def test_a_long_segment_cannot_claim_to_be_connective_to_skip_citing() -> None:
    padding = " ".join(["and"] * (MAX_CONNECTIVE_WORDS + 1))
    with pytest.raises(UnsourcedSegmentError):
        check(segment(padding, cite=False, connective=True))


def test_a_segment_exactly_at_the_limit_is_still_connective() -> None:
    text = " ".join(["and"] * MAX_CONNECTIVE_WORDS)
    [validated] = check(segment(text, cite=False, connective=True)).segments
    assert validated.connective is True


def test_a_long_segment_that_cites_a_claim_is_kept_but_no_longer_called_connective() -> None:
    text = " ".join(["and"] * (MAX_CONNECTIVE_WORDS + 20))
    [validated] = check(segment(text, cite=True, connective=True)).segments
    assert validated.connective is False and validated.claim_verification_ids == (CLAIM,)


def test_a_hand_off_cannot_smuggle_a_fact_in_as_connective() -> None:
    with pytest.raises(UngroundedSegmentError):
        check(
            segment(
                "Next, respiration, which since 1950 has been studied.", cite=False, connective=True
            )
        )


# --- the old behaviour is still what you get without a reference ----------------------------


def test_without_a_grounding_reference_only_the_citation_rules_apply() -> None:
    [validated] = check(segment("A leaf makes 42 grams of sugar."), grounding=None).segments
    assert validated.claim_verification_ids == (CLAIM,)


def test_an_unsupported_or_missing_citation_is_still_caught_first() -> None:
    with pytest.raises(UnsourcedSegmentError):
        check(segment("Plants make food.", cite=False))


def test_the_digit_in_a_chemical_formula_is_not_a_figure() -> None:
    for formula in ("CO2", "H2O", "O2", "C6H12O6", "B12"):
        check(segment(f"The plant takes in {formula} and gives back something useful."))


def test_a_digit_that_stands_alone_is_still_a_figure() -> None:
    assert unsupported("The leaf takes in 2 kinds of gas.") == ["2"]
