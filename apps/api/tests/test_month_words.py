"""Months that are also ordinary words ("may", "march", "august"): written as ordinary words they
are not dates the narration must contain; written as months (capitalised) they are."""

from __future__ import annotations

import pytest

from edutoon.domain.storyboard_provenance import unsupported_tokens

_DATES = frozenset({"date_word"})


@pytest.mark.parametrize(
    "text",
    [
        "Investors may choose a fund.",
        "Returns may differ from one year to the next.",
        "It may be risky.",
        "Investors march towards their goals.",
        "The march of progress is slow.",
        "An august body regulates the market.",
    ],
)
def test_the_lower_case_everyday_word_is_not_a_date(text: str) -> None:
    assert unsupported_tokens(text, "Investors choose funds.", kinds=_DATES) == []


@pytest.mark.parametrize(
    "text",
    [
        "The fund opened in May.",
        "It closes on MAY 5.",
        "March was slow.",
        "It opened in MARCH.",
        "August was slow.",
        "Trading resumed in August.",
    ],
)
def test_a_month_that_the_source_does_not_contain_is_still_refused(text: str) -> None:
    assert unsupported_tokens(text, "Investors choose funds.", kinds=_DATES) != []


@pytest.mark.parametrize("month", ["May", "March", "August"])
def test_a_month_the_source_contains_is_allowed(month: str) -> None:
    text = f"It opened in {month}."
    assert unsupported_tokens(text, f"The fund opened in {month}.", kinds=_DATES) == []


def test_other_months_are_unaffected_by_case() -> None:
    assert unsupported_tokens("It opened in june.", "Investors choose funds.", kinds=_DATES) != []
