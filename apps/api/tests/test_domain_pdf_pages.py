"""``domain.pdf_pages``: which pages need OCR, and what makes recognised text usable."""

from __future__ import annotations

import pytest

from edutoon.domain import pdf_pages


def test_a_page_of_prose_has_text_and_a_page_number_does_not() -> None:
    assert pdf_pages.has_text("Photosynthesis converts light energy into chemical energy.")
    assert not pdf_pages.has_text("12")
    assert not pdf_pages.has_text("- 4 -")
    assert not pdf_pages.has_text("")
    assert not pdf_pages.has_text(None)


def test_the_pages_to_read_are_the_ones_whose_text_layer_is_empty() -> None:
    pages = [
        "A paragraph with plenty of words to count as text.",
        "",
        "7",
        None,
        "More real prose.",
    ]
    assert pdf_pages.pages_without_text(pages) == [1, 2, 3, 4]
    assert pdf_pages.pages_without_text(["Enough letters on this page to be text."]) == []


def test_recognised_lines_are_joined_one_per_line_with_spaces_normalised() -> None:
    assert pdf_pages.tidy_ocr_text(["  The   water cycle ", "", "moves water.  "]) == (
        "The water cycle\nmoves water."
    )


def test_sentences_that_ran_together_are_split_but_abbreviations_are_left_alone() -> None:
    assert pdf_pages.tidy_ocr_text(["droplets that form clouds.When droplets grow"]) == (
        "droplets that form clouds. When droplets grow"
    )
    assert pdf_pages.tidy_ocr_text(["Made in the U.S.A. and by the U.K. Then sold."]) == (
        "Made in the U.S.A. and by the U.K. Then sold."
    )


@pytest.mark.parametrize(
    ("text", "confidence", "readable"),
    [
        ("A clear page of recognised words, easily long enough.", 0.95, True),
        ("A clear page of recognised words, easily long enough.", 0.4, False),
        ("ok", 0.99, False),
        ("", 0.0, False),
    ],
)
def test_a_recognised_page_needs_enough_words_and_a_confident_reading(
    text: str, confidence: float, readable: bool
) -> None:
    assert pdf_pages.page_is_readable(text, confidence) is readable
