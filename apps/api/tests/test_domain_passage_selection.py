"""``domain/topic_tree.select_passages`` - which parts of the material the
model sees. Pure: no fixtures, no DB.

The properties that matter, each pinned below:

- the budget is an explicit argument and is never exceeded;
- the choice is deterministic (same chunks + same budget -> same selection);
- when the material does not fit, the selection is spread across the *whole*
  document - it is not the first N characters.
"""

from __future__ import annotations

import random

import pytest

from edutoon.domain.topic import SourcePassage
from edutoon.domain.topic_tree import (
    DEFAULT_MIN_PASSAGE_CHARS,
    _allocate,
    _even_positions,
    select_passages,
)


def _chunk(i: int, length: int, **kwargs: int | None) -> SourcePassage:
    """A chunk of exactly ``length`` characters with no spaces (so cutting it
    never leaves trailing whitespace to be stripped) that starts with a marker
    unique to ``i``.
    """
    text = (f"[{i}]" + "a" * length)[:length]
    return SourcePassage(chunk_id=f"c{i}", text=text, **kwargs)


def _document(count: int, length: int) -> list[SourcePassage]:
    return [_chunk(i, length) for i in range(count)]


def _ids(selection) -> list[str]:
    return [passage.chunk_id for passage in selection.passages]


def _index(passage: SourcePassage) -> int:
    return int(passage.chunk_id[1:])


# --- when it fits ---------------------------------------------------------------------


def test_a_document_that_fits_is_returned_whole_and_untruncated() -> None:
    chunks = _document(4, 500)

    selection = select_passages(chunks, budget_chars=2000)

    assert selection.passages == tuple(chunks)
    assert selection.truncated is False
    assert (selection.chunks_total, selection.chunks_used) == (4, 4)
    assert (selection.chars_total, selection.chars_used) == (2000, 2000)
    assert not any(passage.excerpt for passage in selection.passages)


def test_a_document_exactly_at_the_budget_is_not_truncated() -> None:
    selection = select_passages(_document(4, 500), budget_chars=2000)

    assert selection.truncated is False


def test_one_character_over_the_budget_is_truncated() -> None:
    selection = select_passages(_document(4, 500), budget_chars=1999)

    assert selection.truncated is True
    assert selection.chars_used <= 1999


def test_no_chunks_selects_nothing_and_is_not_truncated() -> None:
    selection = select_passages([], budget_chars=1000)

    assert selection.passages == ()
    assert (selection.chunks_total, selection.chunks_used) == (0, 0)
    assert selection.truncated is False


def test_chunks_with_no_text_are_ignored() -> None:
    chunks = [SourcePassage("c0", ""), _chunk(1, 100), SourcePassage("c2", "")]

    selection = select_passages(chunks, budget_chars=1000)

    assert _ids(selection) == ["c1"]
    assert selection.chunks_total == 3
    assert selection.chunks_used == 1
    assert selection.truncated is False


def test_all_empty_chunks_select_nothing() -> None:
    selection = select_passages([SourcePassage("c0", "")], budget_chars=1000)

    assert selection.passages == ()
    assert selection.truncated is False


@pytest.mark.parametrize("budget", [0, -1, -500])
def test_a_non_positive_budget_is_rejected(budget: int) -> None:
    with pytest.raises(ValueError, match="budget_chars"):
        select_passages(_document(2, 10), budget_chars=budget)


@pytest.mark.parametrize("minimum", [0, -1])
def test_a_non_positive_minimum_is_rejected(minimum: int) -> None:
    with pytest.raises(ValueError, match="min_chars_per_passage"):
        select_passages(_document(2, 10), budget_chars=100, min_chars_per_passage=minimum)


def test_page_metadata_survives_selection() -> None:
    chunks = [
        _chunk(0, 1000, page_from=1, page_to=1),
        _chunk(1, 1000, page_from=2, page_to=3),
    ]

    fits = select_passages(chunks, budget_chars=5000)
    cut = select_passages(chunks, budget_chars=900)  # both shown, each cut to 450

    assert [(p.page_from, p.page_to) for p in fits.passages] == [(1, 1), (2, 3)]
    assert [(p.page_from, p.page_to) for p in cut.passages] == [(1, 1), (2, 3)]
    assert all(passage.excerpt for passage in cut.passages)


# --- determinism ------------------------------------------------------------------------


def test_the_same_input_and_budget_always_give_the_same_selection() -> None:
    chunks = _document(300, 2000)

    first = select_passages(chunks, budget_chars=48_000)
    second = select_passages(chunks, budget_chars=48_000)
    third = select_passages(list(chunks), budget_chars=48_000)

    assert first == second == third


def test_a_tuple_and_a_list_of_the_same_chunks_select_identically() -> None:
    chunks = _document(80, 1500)

    assert select_passages(tuple(chunks), budget_chars=20_000) == select_passages(
        chunks, budget_chars=20_000
    )


def test_selection_does_not_mutate_its_input() -> None:
    chunks = _document(50, 1000)
    before = list(chunks)

    select_passages(chunks, budget_chars=5000)

    assert chunks == before


def test_selection_is_reproducible_across_many_random_documents() -> None:
    rng = random.Random(20260923)
    for _ in range(50):
        chunks = [_chunk(i, rng.randint(1, 3000)) for i in range(rng.randint(1, 80))]
        budget = rng.randint(1, 30_000)

        assert select_passages(chunks, budget_chars=budget) == select_passages(
            chunks, budget_chars=budget
        )


# --- the budget is preserved ------------------------------------------------------------


@pytest.mark.parametrize("budget", [1, 7, 399, 400, 401, 1000, 9_999, 48_000])
def test_the_budget_is_never_exceeded(budget: int) -> None:
    selection = select_passages(_document(120, 2500), budget_chars=budget)

    assert selection.chars_used <= budget
    assert sum(len(passage.text) for passage in selection.passages) == selection.chars_used


def test_a_truncated_selection_uses_the_whole_budget() -> None:
    """Water-filling hands out every character: nothing is left on the table
    when there is more material than budget (spaceless text, so no trailing
    whitespace is stripped).
    """
    selection = select_passages(_document(120, 2500), budget_chars=30_000)

    assert selection.truncated is True
    assert selection.chars_used == 30_000


def test_the_budget_is_an_argument_not_a_constant() -> None:
    chunks = _document(100, 2000)

    small = select_passages(chunks, budget_chars=10_000)
    large = select_passages(chunks, budget_chars=100_000)

    assert small.chars_used <= 10_000
    assert small.chunks_used < large.chunks_used
    assert large.chars_used <= 100_000
    assert large.truncated is True  # 200_000 characters of material


def test_a_larger_budget_never_selects_fewer_chunks() -> None:
    chunks = _document(200, 1800)

    used = [
        select_passages(chunks, budget_chars=budget).chunks_used
        for budget in (2_000, 8_000, 32_000, 128_000, 512_000)
    ]

    assert used == sorted(used)


# --- whole-document coverage ---------------------------------------------------------------


def _sectioned_document(sections: int, pages_per_section: int, length: int) -> list[SourcePassage]:
    """A long document in clearly labelled sections: page ``i`` belongs to
    section ``i // pages_per_section``.
    """
    return [
        SourcePassage(
            chunk_id=f"c{i}",
            text=(f"SECTION-{i // pages_per_section} page {i} " + "lorem " * length)[:length],
            page_from=i + 1,
            page_to=i + 1,
        )
        for i in range(sections * pages_per_section)
    ]


def test_a_long_multi_section_document_is_covered_from_start_to_end() -> None:
    sections, pages = 5, 60
    chunks = _sectioned_document(sections, pages, length=2000)  # 300 pages, 600k chars

    selection = select_passages(chunks, budget_chars=24_000)

    chosen = [_index(passage) for passage in selection.passages]
    per_section = [sum(1 for i in chosen if i // pages == s) for s in range(sections)]
    assert selection.truncated is True
    assert all(count >= 10 for count in per_section), per_section  # every section is represented
    assert max(per_section) - min(per_section) <= 2, per_section  # and about equally
    assert sum(per_section) == selection.chunks_used


def test_the_selection_is_not_just_the_first_pages() -> None:
    chunks = _document(300, 2000)

    selection = select_passages(chunks, budget_chars=24_000)

    chosen = [_index(passage) for passage in selection.passages]
    assert chosen != list(range(len(chosen)))  # not a prefix of the document
    assert chosen[-1] == 299  # reaches the very end
    assert max(chosen) > len(chosen) * 3  # the spread is genuinely wide


def test_the_first_and_last_chunks_are_always_included() -> None:
    for count in (2, 3, 10, 57, 300):
        selection = select_passages(_document(count, 2000), budget_chars=6_000)

        chosen = [_index(passage) for passage in selection.passages]
        assert chosen[0] == 0, count
        assert chosen[-1] == count - 1, count


def test_no_stretch_of_the_document_is_left_unsampled() -> None:
    """The gap between consecutive chosen chunks never exceeds the even
    spacing (rounded up) - there is no large unsampled stretch anywhere.
    """
    count = 300
    selection = select_passages(_document(count, 2000), budget_chars=24_000)

    chosen = [_index(passage) for passage in selection.passages]
    gaps = [b - a for a, b in zip(chosen, chosen[1:], strict=False)]
    k = len(chosen)
    assert max(gaps) <= -(-(count - 1) // (k - 1))  # ceil((n-1)/(k-1))
    assert min(gaps) >= 1


def test_selected_passages_stay_in_document_order_without_repeats() -> None:
    selection = select_passages(_document(300, 2000), budget_chars=24_000)

    chosen = [_index(passage) for passage in selection.passages]
    assert chosen == sorted(set(chosen))


def test_material_spread_over_several_sources_is_sampled_across_all_of_them() -> None:
    """Chunks from several uploaded sources arrive concatenated in reading
    order; the spread runs across the lot, not within the first source.
    """
    chunks = _document(90, 2000)  # e.g. three 30-page PDFs back to back

    selection = select_passages(chunks, budget_chars=12_000)

    chosen = [_index(passage) for passage in selection.passages]
    by_source = [sum(1 for i in chosen if i // 30 == s) for s in range(3)]
    assert all(count >= 1 for count in by_source), by_source


# --- how the budget is shared ------------------------------------------------------------


def test_every_chunk_is_shown_when_each_can_have_its_minimum() -> None:
    """10 pages of 5000 characters into 8000: each page can have 800, above the
    minimum, so all ten appear - each as a leading excerpt.
    """
    chunks = _document(10, 5000)

    selection = select_passages(chunks, budget_chars=8000)

    assert selection.chunks_used == 10
    assert all(passage.excerpt for passage in selection.passages)
    assert all(len(passage.text) == 800 for passage in selection.passages)
    for passage, chunk in zip(selection.passages, chunks, strict=True):
        assert chunk.text.startswith(passage.text)  # the leading part, not a random slice


def test_short_pages_are_sampled_densely_and_kept_whole() -> None:
    """300 pages of 300 characters into 48,000: every sampled page needs only
    300, so 160 fit - more than a flat 400-per-page rule would allow (120) -
    and none is cut.
    """
    selection = select_passages(_document(300, 300), budget_chars=48_000)

    assert selection.chunks_used == 160
    assert selection.chars_used == 48_000
    assert not any(passage.excerpt for passage in selection.passages)


def test_a_short_chunk_is_kept_whole_and_its_unused_share_goes_to_the_long_ones() -> None:
    chunks = [_chunk(0, 100), _chunk(1, 5000), _chunk(2, 5000), _chunk(3, 100)]

    selection = select_passages(chunks, budget_chars=3000)

    assert [len(passage.text) for passage in selection.passages] == [100, 1400, 1400, 100]
    assert [passage.excerpt for passage in selection.passages] == [False, True, True, False]
    assert selection.chars_used == 3000


def test_leftover_characters_go_one_each_to_the_earliest_truncated_chunks() -> None:
    chunks = [_chunk(0, 1000), _chunk(1, 1000), _chunk(2, 1000)]

    selection = select_passages(chunks, budget_chars=2000, min_chars_per_passage=100)

    assert [len(passage.text) for passage in selection.passages] == [667, 667, 666]


def test_a_larger_minimum_shows_fewer_chunks() -> None:
    chunks = _document(100, 2000)

    lean = select_passages(chunks, budget_chars=20_000, min_chars_per_passage=100)
    generous = select_passages(chunks, budget_chars=20_000, min_chars_per_passage=1000)

    assert lean.chunks_used == 100
    assert generous.chunks_used == 20


def test_the_default_minimum_is_used_when_none_is_given() -> None:
    chunks = _document(300, 2000)

    default = select_passages(chunks, budget_chars=24_000)
    explicit = select_passages(
        chunks, budget_chars=24_000, min_chars_per_passage=DEFAULT_MIN_PASSAGE_CHARS
    )

    assert default == explicit


def test_a_budget_smaller_than_the_minimum_shows_the_opening_cut_to_the_budget() -> None:
    selection = select_passages(_document(10, 2000), budget_chars=150)

    assert _ids(selection) == ["c0"]
    assert len(selection.passages[0].text) == 150
    assert selection.passages[0].excerpt is True
    assert selection.truncated is True


def test_a_single_chunk_longer_than_the_budget_is_cut_to_it() -> None:
    selection = select_passages([_chunk(0, 5000)], budget_chars=1000)

    assert len(selection.passages[0].text) == 1000
    assert selection.passages[0].excerpt is True
    assert (selection.chunks_total, selection.chunks_used) == (1, 1)


def test_an_excerpt_is_the_start_of_the_chunk_and_flagged() -> None:
    chunks = _document(3, 3000)

    selection = select_passages(chunks, budget_chars=3000)

    for passage in selection.passages:
        original = chunks[_index(passage)]
        assert passage.excerpt is True
        assert original.text.startswith(passage.text)
        assert passage.chunk_id == original.chunk_id


def test_trailing_whitespace_from_a_cut_is_stripped_and_still_within_budget() -> None:
    text = "word " * 2000
    chunks = [SourcePassage(f"c{i}", text) for i in range(4)]

    selection = select_passages(chunks, budget_chars=1001)

    assert all(not passage.text.endswith(" ") for passage in selection.passages)
    assert selection.chars_used <= 1001


def test_totals_describe_the_whole_document_even_when_truncated() -> None:
    selection = select_passages(_document(300, 2000), budget_chars=24_000)

    assert selection.chunks_total == 300
    assert selection.chars_total == 600_000
    assert selection.chunks_used < 300
    assert selection.chars_used < 600_000


# --- invariants over many random documents ------------------------------------------------


@pytest.mark.parametrize("seed", range(8))
def test_invariants_hold_for_random_documents(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(60):
        count = rng.randint(1, 70)
        chunks = [_chunk(i, rng.randint(1, 3000)) for i in range(count)]
        budget = rng.randint(1, 25_000)
        minimum = rng.choice([1, 50, 400, 1000])
        total = sum(len(chunk.text) for chunk in chunks)

        selection = select_passages(chunks, budget_chars=budget, min_chars_per_passage=minimum)

        chosen = [_index(passage) for passage in selection.passages]
        assert selection.chars_used <= budget
        assert chosen == sorted(set(chosen))  # document order, no repeats
        assert (selection.chunks_total, selection.chars_total) == (count, total)
        if total <= budget:
            assert selection.truncated is False
            assert chosen == list(range(count))
            continue
        assert selection.truncated is True
        assert chosen[0] == 0
        if len(chosen) > 1:
            assert chosen[-1] == count - 1
        for passage in selection.passages:
            original = chunks[_index(passage)]
            assert original.text.startswith(passage.text)
            # Nobody is starved below the minimum (or the whole chunk / budget).
            assert len(passage.text) >= min(len(original.text), minimum, budget)


# --- the two helpers the rule is built from -------------------------------------------------


@pytest.mark.parametrize(("count", "k"), [(10, 1), (10, 2), (10, 5), (10, 10), (300, 60), (7, 3)])
def test_even_positions_are_distinct_increasing_and_span_the_range(count: int, k: int) -> None:
    positions = _even_positions(count, k)

    assert len(positions) == k
    assert positions == sorted(set(positions))
    assert positions[0] == 0
    if k > 1:
        assert positions[-1] == count - 1


def test_even_positions_choosing_everything_is_the_identity() -> None:
    assert _even_positions(9, 9) == list(range(9))


def test_even_positions_choosing_one_takes_the_first() -> None:
    assert _even_positions(50, 1) == [0]


def test_even_positions_are_exact_integer_arithmetic() -> None:
    # 5 evenly from 0..9: 0, 2.25, 4.5, 6.75, 9 rounded half up.
    assert _even_positions(10, 5) == [0, 2, 5, 7, 9]


@pytest.mark.parametrize(
    ("lengths", "budget", "expected"),
    [
        ([100, 100], 500, [100, 100]),  # fits: untouched
        ([100, 5000, 5000, 100], 3000, [100, 1400, 1400, 100]),
        ([1000, 1000, 1000], 2000, [667, 667, 666]),
        ([10, 20, 5000], 1000, [10, 20, 970]),
        ([5000], 1200, [1200]),
        ([300, 300, 300], 700, [234, 233, 233]),
    ],
)
def test_allocate_is_water_filling(lengths: list[int], budget: int, expected: list[int]) -> None:
    allocation = _allocate(lengths, budget)

    assert allocation == expected
    assert all(a <= length for a, length in zip(allocation, lengths, strict=True))
    if sum(lengths) > budget:
        assert sum(allocation) == budget
