"""``domain/script_openers.py`` - pure.

Regression, from real scripts: four of ten sections opened "Today, we will learn about ..." (and a
topic-only lesson said "Today, we have learned about Newton's third law" a few lines before its
closing), in spite of a prompt that asks the writer not to.
"""

from __future__ import annotations

from uuid import uuid4

from edutoon.domain.script import ScriptSegmentDraft
from edutoon.domain.script_openers import (
    drop_repeated_openers,
    is_announcement,
    is_premature_wrap_up,
)

WPM = 150


def _segment(topic, text: str, index: int = 0) -> ScriptSegmentDraft:
    return ScriptSegmentDraft(
        segment_index=index,
        narration_text=text,
        claim_ids=[],
        estimated_duration_ms=1000,
        topic_id=topic,
    )


def test_announcements_in_the_forms_real_writers_use_are_recognised():
    for sentence in (
        "Today, we will learn about resistance.",
        "Today, we'll learn about Newton's first law of motion.",
        "Now, let's talk about parallel circuits.",
        "In this lesson, we'll learn about adding resistors in parallel.",
        "Today, we will discuss Newton's third law of motion.",
        "Next, we will look at how humans change ecosystems.",
    ):
        assert is_announcement(sentence), sentence


def test_a_statement_is_not_an_announcement():
    for sentence in (
        "Resistance is measured in ohms.",
        "Today the Moon is about 384,000 kilometres away.",
        "First, let's understand what force is.",
        "We covered everything from Electric Circuits to Electrical Safety.",
    ):
        assert not is_announcement(sentence), sentence


def test_a_wrap_up_before_the_end_is_recognised():
    assert is_premature_wrap_up("Today, we have learned about Newton's third law of motion.")
    assert not is_premature_wrap_up("That brings us to the end of this lesson.")


def test_the_first_announcement_stays_and_later_ones_go():
    one, two, three = uuid4(), uuid4(), uuid4()
    segments = [
        _segment(one, "Today, we will learn about electric circuits."),
        _segment(one, "Electric current is the flow of electric charge.", 1),
        _segment(two, "Today, we will learn about resistance. Resistance is measured in ohms.", 2),
        _segment(three, "Today, we will learn about Ohm's law.", 3),
        _segment(three, "Voltage equals current multiplied by resistance.", 4),
    ]

    kept, removed = drop_repeated_openers(segments, words_per_minute=WPM)

    assert [s.narration_text for s in kept] == [
        "Today, we will learn about electric circuits.",
        "Electric current is the flow of electric charge.",
        "Resistance is measured in ohms.",
        "Voltage equals current multiplied by resistance.",
    ]
    assert removed == 2


def test_a_section_is_never_left_without_a_passage():
    one, two = uuid4(), uuid4()
    segments = [
        _segment(one, "Today, we will learn about circuits."),
        _segment(two, "Today, we will learn about resistance."),
    ]

    kept, removed = drop_repeated_openers(segments, words_per_minute=WPM)

    assert [s.narration_text for s in kept] == [
        "Today, we will learn about circuits.",
        "Today, we will learn about resistance.",
    ]
    assert removed == 0


def test_a_premature_wrap_up_goes_even_when_it_is_the_only_thing_left_to_say():
    one, two = uuid4(), uuid4()
    segments = [
        _segment(one, "Force is a push or pull on an object."),
        _segment(two, "Every action has an equal and opposite reaction.", 1),
        _segment(two, "Today, we have learned about Newton's third law of motion.", 2),
    ]

    kept, removed = drop_repeated_openers(segments, words_per_minute=WPM)

    assert [s.narration_text for s in kept] == [
        "Force is a push or pull on an object.",
        "Every action has an equal and opposite reaction.",
    ]
    assert removed == 1


def test_a_shortened_passage_gets_its_duration_recomputed():
    one = uuid4()
    segments = [
        _segment(one, "Today, we will learn about circuits."),
        _segment(one, "Today, we will learn about resistance. Resistance is measured in ohms.", 1),
    ]

    kept, _removed = drop_repeated_openers(segments, words_per_minute=WPM)

    assert kept[1].narration_text == "Resistance is measured in ohms."
    assert kept[1].estimated_duration_ms == round(5 / WPM * 60_000)
