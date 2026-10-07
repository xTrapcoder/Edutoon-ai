"""PowerPoint parsing (``providers.pptx``): structure kept, hidden slides skipped, and anything
that is not a readable deck refused with one error type."""

from __future__ import annotations

import io
import os
import zipfile

import pytest

from edutoon.providers import pptx
from tests.pdf_helpers import make_pdf
from tests.pptx_helpers import BLANK, SlideSpec, make_deck, sample_training_deck


def test_a_deck_is_read_slide_by_slide_with_its_own_slide_numbers():
    deck = pptx.extract_deck(sample_training_deck())
    assert deck.title == "Workplace Safety Induction"
    assert deck.slide_count == 13  # the hidden slide counts as a slide the deck has
    assert [s.index for s in deck.slides] == list(range(1, 13))  # but is not read
    assert all(s.deck_title == "Workplace Safety Induction" for s in deck.slides)


def test_bullets_keep_their_level_and_numbered_steps_stay_numbered():
    slides = {s.title: s for s in pptx.extract_deck(sample_training_deck()).slides}
    kinds = [(b.kind, b.level) for b in slides["Common Hazards"].blocks]
    assert kinds == [("bullet", 0), ("bullet", 1), ("bullet", 1), ("bullet", 0), ("bullet", 0)]
    assert {b.kind for b in slides["If You Discover a Fire"].blocks} == {"numbered", "notes"}


def test_a_table_and_speaker_notes_are_read():
    slides = {s.title: s for s in pptx.extract_deck(sample_training_deck()).slides}
    [table] = [b for b in slides["Choosing the Right Extinguisher"].blocks if b.kind == "table"]
    assert table.rows[0] == ("Type", "Use it on", "Never use it on")
    assert table.rows[2] == ("CO2", "Electrical equipment", "Metal fires")
    assert slides["Thank You"].notes == "Questions are welcome."


def test_a_plain_text_box_is_a_paragraph_and_a_placeholder_body_is_bullets():
    deck = pptx.extract_deck(
        make_deck(
            [SlideSpec("Mixed", bullets=[("Point one", 0)], paragraph="Some plain text here.")]
        )
    )
    assert [b.kind for b in deck.slides[0].blocks] == ["bullet", "paragraph"]


def test_a_slide_with_no_text_is_read_but_has_nothing_to_say():
    deck = pptx.extract_deck(
        make_deck([SlideSpec(layout=BLANK), SlideSpec("Real", bullets=[("Point", 0)])])
    )
    assert [s.index for s in deck.slides] == [1, 2]
    assert deck.slides[0].blocks == () and deck.slides[0].title is None


def test_a_deck_over_the_slide_limit_is_refused_before_any_text_is_read():
    content = make_deck([SlideSpec(f"S{i}", bullets=[("x y z", 0)]) for i in range(5)])
    with pytest.raises(pptx.PptxTooManySlidesError) as raised:
        pptx.extract_deck(content, max_slides=3)
    assert (raised.value.slide_count, raised.value.max_slides) == (5, 3)


@pytest.mark.parametrize(
    "content",
    [b"", b"not a presentation", os.urandom(300), b"PK\x03\x04" + os.urandom(200), make_pdf()],
)
def test_anything_that_is_not_a_readable_deck_is_one_error(content):
    with pytest.raises(pptx.PptxParseError):
        pptx.extract_deck(content)


def test_a_zip_that_is_not_a_presentation_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")
    assert not pptx.looks_like_pptx(buffer.getvalue())
    with pytest.raises(pptx.PptxParseError):
        pptx.extract_deck(buffer.getvalue())


def test_a_zip_that_would_expand_unreasonably_is_refused_unread(monkeypatch):
    monkeypatch.setattr(pptx, "MAX_UNCOMPRESSED_BYTES", 1000)
    with pytest.raises(pptx.PptxParseError, match="unreasonable size"):
        pptx.extract_deck(sample_training_deck())


def test_looks_like_pptx_tells_a_deck_from_other_files():
    assert pptx.looks_like_pptx(sample_training_deck())
    assert not pptx.looks_like_pptx(make_pdf())
    assert not pptx.looks_like_pptx(b"")
