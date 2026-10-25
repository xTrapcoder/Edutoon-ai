"""``domain.pasted_text``: cleaning, bounds and chunking of text a project is built from."""

from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.domain import pasted_text as pt
from edutoon.domain.source_storage import belongs_to_project, source_storage_key

PROSE = (
    "Photosynthesis is how plants make food from light, water and carbon dioxide. "
    "It happens in the chloroplasts of a leaf cell. "
) * 8


def _words(text: str) -> list[str]:
    return text.split()


# --- clean -----------------------------------------------------------------------------------


def test_clean_unifies_newlines_and_removes_control_characters():
    assert pt.clean("a\r\nb\rc\x00d\x07e") == "a\nb\ncde"


def test_clean_trims_line_ends_and_collapses_blank_runs():
    assert pt.clean("  one  \n\n\n\n\ntwo   \n") == "one\n\ntwo"


def test_clean_keeps_tabs_and_non_ascii():
    assert pt.clean("café\tnaïve – 5 °C") == "café\tnaïve – 5 °C"


@pytest.mark.parametrize("raw", ["", "  \n ", "x\r\n\r\n\r\ny \x00", PROSE, "a\n\n\n\nb"])
def test_clean_is_idempotent(raw):
    assert pt.clean(pt.clean(raw)) == pt.clean(raw)


def test_crlf_and_lf_texts_clean_to_the_same_text():
    assert pt.clean("a\r\n\r\nb") == pt.clean("a\n\nb")


# --- validate --------------------------------------------------------------------------------


def test_text_at_the_minimum_is_accepted_and_one_short_is_not():
    pt.validate("a" * pt.MIN_CHARS)
    with pytest.raises(pt.PastedTextError, match="too short"):
        pt.validate("a" * (pt.MIN_CHARS - 1))


def test_text_at_the_maximum_is_accepted_and_one_over_is_not():
    pt.validate("a" * pt.MAX_CHARS)
    with pytest.raises(pt.PastedTextError, match="too long"):
        pt.validate("a" * (pt.MAX_CHARS + 1))


def test_text_with_no_letters_is_refused_whatever_its_length():
    with pytest.raises(pt.PastedTextError, match="no words"):
        pt.validate("1234 5678 ... " * 100)


def test_the_limits_are_the_agreed_ones():
    assert (pt.MIN_CHARS, pt.MAX_CHARS) == (300, 200_000)


# --- split_chunks ----------------------------------------------------------------------------


def test_short_text_is_one_chunk():
    assert pt.split_chunks("One paragraph.\n\nAnother.") == ["One paragraph.\n\nAnother."]


def test_paragraphs_are_packed_together_while_they_fit_and_never_split_needlessly():
    paragraphs = [f"Paragraph {i}. " + "word " * 60 for i in range(20)]
    chunks = pt.split_chunks("\n\n".join(p.strip() for p in paragraphs), limit=1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert len(chunks) > 1
    # a chunk break falls between whole paragraphs
    assert all(chunk.split("\n\n")[0].startswith("Paragraph") for chunk in chunks)


def test_every_chunk_is_non_empty_and_within_the_limit_and_no_word_is_lost():
    text = pt.clean("\n\n".join(PROSE for _ in range(40)))
    chunks = pt.split_chunks(text)
    assert chunks and all(0 < len(c) <= pt.CHUNK_CHARS for c in chunks)
    assert _words("\n\n".join(chunks)) == _words(text)


def test_one_long_paragraph_is_cut_at_sentence_ends():
    sentence = "This is a fairly ordinary sentence about cells. "
    text = (sentence * 200).strip()
    chunks = pt.split_chunks(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert all(c.endswith(".") for c in chunks)
    assert _words(" ".join(chunks)) == _words(text)


def test_a_sentence_longer_than_the_limit_is_cut_at_spaces_not_in_words():
    text = " ".join(f"word{i}" for i in range(400))
    chunks = pt.split_chunks(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert _words(" ".join(chunks)) == _words(text)


def test_a_word_longer_than_the_limit_is_hard_cut_and_loses_nothing():
    text = "x" * 1050
    chunks = pt.split_chunks(text, limit=100)
    assert all(0 < len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_chunking_is_deterministic():
    text = pt.clean("\n\n".join(PROSE for _ in range(30)))
    assert pt.split_chunks(text) == pt.split_chunks(text)


def test_a_limit_below_one_is_a_programming_error():
    with pytest.raises(ValueError):
        pt.split_chunks("text", limit=0)


# --- storage key -----------------------------------------------------------------------------


def test_pasted_text_is_stored_as_txt_inside_the_projects_own_prefix():
    project = uuid4()
    key = source_storage_key(project, "a" * 64, "txt")
    assert key == f"projects/{project}/{'a' * 64}.txt"
    assert belongs_to_project(key, project)
    assert not belongs_to_project(key, uuid4())


def test_the_default_storage_key_is_still_a_pdf():
    assert source_storage_key(uuid4(), "b" * 64).endswith(".pdf")
