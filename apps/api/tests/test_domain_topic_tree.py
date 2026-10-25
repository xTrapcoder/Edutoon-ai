"""``domain/topic_tree.py`` - keyword rules, mode resolution, draft validation
and hierarchy rules. Pure: no fixtures, no DB, no provider.

``validate_and_build`` is the domain's answer to "what may the model's topic
proposal become?", so most tests here are about what it *refuses to trust*:
evidence the model was never shown, an origin it claims for itself, a place in
the hierarchy it was not entitled to, a user keyword it tried to rewrite.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from edutoon.domain.topic import (
    TopicDraft,
    TopicLimits,
    TopicNodeDraft,
    TopicSeed,
    ValidatedTopicTree,
)
from edutoon.domain.topic_tree import (
    CyclicHierarchyError,
    DuplicateRefError,
    InvalidKeywordError,
    InvalidTopicError,
    NoTopicInputError,
    NoTopicsError,
    TopicDepthError,
    TopicError,
    TopicLimitError,
    TopicOrderError,
    TopicTreeError,
    UnknownParentError,
    accepted_topics,
    evidence_count_is_valid,
    normalise_term,
    normalise_user_keywords,
    reading_order,
    resolve_input_mode,
    term_key,
    validate_and_build,
    validate_hierarchy,
)

LIMITS = TopicLimits(
    max_topics=30,
    max_depth=3,
    max_title_chars=60,
    max_summary_chars=100,
    max_keywords_per_topic=4,
    max_keyword_chars=20,
    max_evidence_per_topic=3,
)


# Characters that are easy to mistake for others (or invisible) are named, not typed.
COMBINING_ACUTE = chr(0x0301)
E_ACUTE = chr(0x00E9)
NBSP = chr(0x00A0)
FI_LIGATURE = chr(0xFB01)  # NFKC-normalises to "fi"


# --- builders ------------------------------------------------------------------------


def _ids(count: int) -> list[UUID]:
    return [uuid4() for _ in range(count)]


def _node(
    ref: str,
    title: str | None = None,
    parent: str | None = None,
    *,
    summary: str | None = None,
    keywords: Sequence[str] = (),
    evidence: Sequence[UUID | str] = (),
    seed: str | None = None,
) -> TopicNodeDraft:
    return TopicNodeDraft(
        ref=ref,
        title=ref.upper() if title is None else title,
        parent_ref=parent,
        summary=summary,
        keywords=tuple(keywords),
        evidence_chunk_ids=tuple(str(item) for item in evidence),
        seed_ref=seed,
    )


def _build(
    *nodes: TopicNodeDraft,
    seeds: Sequence[TopicSeed] = (),
    known: Sequence[UUID] = (),
    limits: TopicLimits = LIMITS,
    first_root_position: int | None = None,
) -> ValidatedTopicTree:
    return validate_and_build(
        TopicDraft(nodes=tuple(nodes)),
        seeds=tuple(seeds),
        known_chunk_ids=set(known),
        limits=limits,
        first_root_position=first_root_position,
    )


def _titles(tree: ValidatedTopicTree) -> list[str]:
    return [node.title for node in tree.nodes]


def _by_title(tree: ValidatedTopicTree, title: str):
    [match] = [node for node in tree.nodes if node.title == title]
    return match


@dataclass(frozen=True)
class Row:
    """A stored topic, as the hierarchy rules see one (satisfies ``TopicLike``)."""

    id: UUID
    parent_id: UUID | None
    depth: int
    position: int
    status: str = "proposed"


def _uuid(n: int) -> UUID:
    return UUID(int=n)


# --- keywords ------------------------------------------------------------------------


def test_normalise_term_collapses_whitespace_and_applies_nfkc() -> None:
    assert normalise_term("  Photo \t synthesis\n") == "Photo synthesis"
    assert normalise_term("ＡＢＣ") == "ABC"  # full-width -> ASCII
    assert normalise_term("e" + COMBINING_ACUTE) == E_ACUTE  # accent + letter -> one character
    assert normalise_term(NBSP) == ""  # a non-breaking space is only whitespace


def test_term_key_ignores_case_and_spacing_only() -> None:
    assert term_key("Photosynthesis") == term_key("  photosynthesis ")
    assert term_key("Light reactions") == term_key("LIGHT   REACTIONS")
    assert term_key("Light reactions") != term_key("Dark reactions")


def test_user_keywords_are_trimmed_and_normalised() -> None:
    assert normalise_user_keywords(
        ["  Photo   synthesis ", "ＡＴＰ"], max_keywords=10, max_length=50
    ) == ["Photo synthesis", "ATP"]


def test_user_keywords_deduplicate_case_insensitively_keeping_the_first_spelling() -> None:
    assert normalise_user_keywords(
        ["Chlorophyll", "chlorophyll", "  CHLOROPHYLL ", "Light"], max_keywords=10, max_length=50
    ) == ["Chlorophyll", "Light"]


def test_user_keywords_treat_unicode_equivalents_as_duplicates() -> None:
    cleaned = normalise_user_keywords([FI_LIGATURE + "ne", "fine"], max_keywords=10, max_length=50)

    assert cleaned == ["fine"]


def test_user_keywords_keep_their_order_and_unicode() -> None:
    assert normalise_user_keywords(
        ["zeta", "Éclair", "日本語", "alpha"], max_keywords=10, max_length=50
    ) == ["zeta", "Éclair", "日本語", "alpha"]


def test_no_keywords_is_an_empty_list_not_an_error() -> None:
    assert normalise_user_keywords([], max_keywords=5, max_length=20) == []


@pytest.mark.parametrize("blank", ["", "   ", "\t\n", NBSP])
def test_a_blank_user_keyword_is_rejected(blank: str) -> None:
    with pytest.raises(InvalidKeywordError, match="blank"):
        normalise_user_keywords(["ok", blank], max_keywords=5, max_length=20)


def test_an_over_long_user_keyword_is_rejected_but_exactly_the_limit_is_fine() -> None:
    assert normalise_user_keywords(["x" * 20], max_keywords=5, max_length=20) == ["x" * 20]

    with pytest.raises(InvalidKeywordError, match="limit is 20"):
        normalise_user_keywords(["x" * 21], max_keywords=5, max_length=20)


def test_too_many_distinct_user_keywords_are_rejected_but_duplicates_do_not_count() -> None:
    assert normalise_user_keywords(["a", "A", "b", "B", "c"], max_keywords=3, max_length=5) == [
        "a",
        "b",
        "c",
    ]

    with pytest.raises(InvalidKeywordError, match="limit is 3"):
        normalise_user_keywords(["a", "b", "c", "d"], max_keywords=3, max_length=5)


@pytest.mark.parametrize(("max_keywords", "max_length"), [(0, 5), (5, 0), (-1, -1)])
def test_user_keyword_limits_must_be_positive(max_keywords: int, max_length: int) -> None:
    with pytest.raises(ValueError):
        normalise_user_keywords(["a"], max_keywords=max_keywords, max_length=max_length)


# --- input mode & evidence rule ----------------------------------------------------------


@pytest.mark.parametrize(
    ("has_passages", "has_seeds", "expected"),
    [
        (True, False, "pdf"),
        (False, True, "keywords"),
        (True, True, "pdf_keywords"),
    ],
)
def test_the_input_mode_follows_from_the_data_present(
    has_passages: bool, has_seeds: bool, expected: str
) -> None:
    assert resolve_input_mode(has_passages=has_passages, has_seeds=has_seeds) == expected


def test_no_material_and_no_keywords_is_an_error() -> None:
    with pytest.raises(NoTopicInputError, match="nothing to extract"):
        resolve_input_mode(has_passages=False, has_seeds=False)


@pytest.mark.parametrize(
    ("origin", "count", "valid"),
    [
        ("source", 0, False),
        ("source", 1, True),
        ("source", 5, True),
        ("suggested", 0, True),
        ("suggested", 1, False),
        ("user", 0, True),
        ("user", 3, True),
        ("model", 0, False),
        ("", 1, False),
    ],
)
def test_the_evidence_invariant_per_origin(origin: str, count: int, valid: bool) -> None:
    assert evidence_count_is_valid(origin, count) is valid


def test_every_topic_error_shares_one_base_class() -> None:
    for error in (
        NoTopicInputError,
        InvalidKeywordError,
        InvalidTopicError,
        NoTopicsError,
        TopicTreeError,
        DuplicateRefError,
        UnknownParentError,
        CyclicHierarchyError,
        TopicDepthError,
        TopicOrderError,
        TopicLimitError,
    ):
        assert issubclass(error, TopicError)
    for tree_error in (
        DuplicateRefError,
        UnknownParentError,
        CyclicHierarchyError,
        TopicDepthError,
        TopicOrderError,
        TopicLimitError,
    ):
        assert issubclass(tree_error, TopicTreeError)


# --- validate_and_build: PDF only --------------------------------------------------------


def test_a_well_cited_tree_is_built_in_reading_order_with_depth_and_position_assigned() -> None:
    ids = _ids(3)
    tree = _build(
        _node("a", "Photosynthesis", evidence=[ids[0]]),
        _node("a1", "Light reactions", parent="a", evidence=[ids[1]]),
        _node("b", "Respiration", evidence=[ids[2]]),
        _node("a2", "Calvin cycle", parent="a", evidence=[ids[0]]),
        _node("b1", "Glycolysis", parent="b", evidence=[ids[2]]),
        known=ids,
    )

    assert tree.mode == "pdf"
    assert tree.dropped_uncited == 0
    assert _titles(tree) == [
        "Photosynthesis",
        "Light reactions",
        "Calvin cycle",
        "Respiration",
        "Glycolysis",
    ]
    assert [n.index for n in tree.nodes] == [0, 1, 2, 3, 4]
    assert [n.parent_index for n in tree.nodes] == [None, 0, 0, None, 3]
    assert [n.depth for n in tree.nodes] == [0, 1, 1, 0, 1]
    assert [n.position for n in tree.nodes] == [0, 0, 1, 1, 0]
    assert {n.origin for n in tree.nodes} == {"source"}
    assert all(n.seed_ref is None for n in tree.nodes)


def test_a_child_may_appear_before_its_parent_in_the_draft() -> None:
    ids = _ids(2)

    tree = _build(
        _node("child", "Child", parent="root", evidence=[ids[1]]),
        _node("root", "Root", evidence=[ids[0]]),
        known=ids,
    )

    assert _titles(tree) == ["Root", "Child"]
    assert [n.depth for n in tree.nodes] == [0, 1]
    assert tree.nodes[1].parent_index == 0


def test_siblings_are_numbered_from_zero_in_the_order_the_model_gave_them() -> None:
    ids = _ids(1)
    tree = _build(
        _node("r", "Root", evidence=ids),
        _node("c", "Third", parent="r", evidence=ids),
        _node("a", "First", parent="r", evidence=ids),
        _node("b", "Second", parent="r", evidence=ids),
        known=ids,
    )

    children = [n for n in tree.nodes if n.parent_index == 0]
    assert [(n.title, n.position) for n in children] == [
        ("Third", 0),
        ("First", 1),
        ("Second", 2),
    ]


def test_new_roots_are_numbered_from_first_root_position() -> None:
    ids = _ids(1)

    tree = _build(
        _node("a", evidence=ids), _node("b", evidence=ids), known=ids, first_root_position=5
    )

    assert [n.position for n in tree.nodes] == [5, 6]


def test_cited_ids_are_narrowed_to_the_chunks_the_model_was_shown() -> None:
    shown, never_shown = _ids(2), _ids(1)

    tree = _build(
        _node("a", evidence=[shown[0], never_shown[0], "not-a-uuid", "", shown[1]]),
        known=shown,
    )

    assert tree.nodes[0].evidence_chunk_ids == (shown[0], shown[1])


def test_cited_ids_are_deduplicated_keeping_first_seen_order() -> None:
    ids = _ids(3)

    tree = _build(_node("a", evidence=[ids[2], ids[0], ids[2], ids[1], ids[0]]), known=ids)

    assert tree.nodes[0].evidence_chunk_ids == (ids[2], ids[0], ids[1])


def test_cited_ids_are_capped_per_topic_keeping_the_first_ones() -> None:
    ids = _ids(5)

    tree = _build(_node("a", evidence=ids), known=ids)  # limit is 3

    assert tree.nodes[0].evidence_chunk_ids == tuple(ids[:3])


def test_a_topic_citing_only_ids_it_was_never_shown_counts_as_uncited_and_is_dropped() -> None:
    shown = _ids(1)

    tree = _build(
        _node("good", evidence=shown),
        _node("bad", evidence=[uuid4()]),
        known=shown,
    )

    assert _titles(tree) == ["GOOD"]
    assert tree.dropped_uncited == 1


def test_the_origin_of_a_cited_topic_is_source_whatever_the_model_might_have_hoped() -> None:
    ids = _ids(1)

    tree = _build(_node("a", evidence=ids), known=ids)

    assert tree.nodes[0].origin == "source"


def test_an_uncited_parent_is_dropped_and_its_cited_children_move_up_a_level() -> None:
    ids = _ids(1)

    tree = _build(
        _node("p", "Uncited parent"),
        _node("c", "Cited child", parent="p", evidence=ids),
        _node("g", "Cited grandchild", parent="c", evidence=ids),
        known=ids,
    )

    assert tree.dropped_uncited == 1
    assert _titles(tree) == ["Cited child", "Cited grandchild"]
    assert [(n.depth, n.parent_index) for n in tree.nodes] == [(0, None), (1, 0)]
    assert [n.position for n in tree.nodes] == [0, 0]


def test_an_uncited_middle_topic_is_dropped_and_the_child_attaches_to_the_grandparent() -> None:
    ids = _ids(1)

    tree = _build(
        _node("r", "Root", evidence=ids),
        _node("m", "Uncited middle", parent="r"),
        _node("c", "Child", parent="m", evidence=ids),
        known=ids,
    )

    assert _titles(tree) == ["Root", "Child"]
    assert tree.nodes[1].parent_index == 0
    assert tree.nodes[1].depth == 1


def test_a_run_of_uncited_ancestors_is_skipped_entirely() -> None:
    ids = _ids(1)

    tree = _build(
        _node("r", "Root", evidence=ids),
        _node("x", "Uncited 1", parent="r"),
        _node("y", "Uncited 2", parent="x"),
        _node("c", "Child", parent="y", evidence=ids),
        known=ids,
    )

    assert tree.dropped_uncited == 2
    assert tree.nodes[1].title == "Child"
    assert tree.nodes[1].parent_index == 0


def test_a_run_where_nothing_is_cited_is_an_error_not_an_empty_success() -> None:
    with pytest.raises(NoTopicsError):
        _build(_node("a"), _node("b", parent="a"), known=_ids(2))


def test_an_empty_draft_with_material_is_an_error() -> None:
    with pytest.raises(NoTopicsError):
        _build(known=_ids(2))


def test_no_material_and_no_seeds_is_no_input_at_all() -> None:
    with pytest.raises(NoTopicInputError):
        _build(_node("a"))


# --- depth, size and structure --------------------------------------------------------------


def test_a_chain_exactly_max_depth_levels_deep_is_allowed() -> None:
    ids = _ids(1)

    tree = _build(
        _node("a", evidence=ids),
        _node("b", parent="a", evidence=ids),
        _node("c", parent="b", evidence=ids),
        known=ids,
    )  # LIMITS.max_depth is 3 levels: depths 0, 1, 2

    assert [n.depth for n in tree.nodes] == [0, 1, 2]


def test_a_chain_one_level_too_deep_is_trimmed_not_failed() -> None:
    """A model that nests one level too deep keeps its first three levels; the over-deep topic is
    dropped and counted, never the whole extraction failed for it."""
    ids = _ids(1)

    tree = _build(
        _node("a", evidence=ids),
        _node("b", parent="a", evidence=ids),
        _node("c", parent="b", evidence=ids),
        _node("d", parent="c", evidence=ids),
        known=ids,
    )

    assert _titles(tree) == ["A", "B", "C"]
    assert [n.depth for n in tree.nodes] == [0, 1, 2]
    assert tree.dropped_over_limit == 1


def test_trimming_for_depth_moves_a_dropped_topics_children_up() -> None:
    ids = _ids(1)

    tree = _build(
        _node("a", evidence=ids),
        _node("b", parent="a", evidence=ids),
        _node("c", parent="b", evidence=ids),
        _node("d", parent="c", evidence=ids),
        _node("e", parent="d", evidence=ids),
        known=ids,
    )

    assert _titles(tree) == ["A", "B", "C"]
    assert tree.dropped_over_limit == 2


def test_dropping_an_uncited_ancestor_can_bring_a_deep_chain_back_within_the_limit() -> None:
    """Depth is measured on the tree that will actually be stored, after
    uncited topics are removed - not on the model's original nesting.
    """
    ids = _ids(1)

    tree = _build(
        _node("a", evidence=ids),
        _node("b", parent="a"),  # uncited: dropped
        _node("c", parent="b", evidence=ids),
        _node("d", parent="c", evidence=ids),
        known=ids,
    )

    assert [n.depth for n in tree.nodes] == [0, 1, 2]


def test_a_tree_at_the_topic_limit_is_allowed_and_one_more_is_rejected() -> None:
    ids = _ids(1)
    small = TopicLimits(
        max_topics=3,
        max_depth=3,
        max_title_chars=60,
        max_summary_chars=100,
        max_keywords_per_topic=4,
        max_keyword_chars=20,
        max_evidence_per_topic=3,
    )
    three = [_node(f"n{i}", evidence=ids) for i in range(3)]

    assert len(_build(*three, known=ids, limits=small).nodes) == 3
    over = _build(*three, _node("n3", evidence=ids), known=ids, limits=small)
    assert _titles(over) == ["N0", "N1", "N2"]
    assert over.dropped_over_limit == 1


def test_too_many_topics_drops_the_deepest_then_the_last_but_never_a_user_topic() -> None:
    ids = _ids(1)
    small = TopicLimits(
        max_topics=3,
        max_depth=3,
        max_title_chars=60,
        max_summary_chars=100,
        max_keywords_per_topic=4,
        max_keyword_chars=20,
        max_evidence_per_topic=3,
    )
    seed = TopicSeed(ref="s1", title="Calvin cycle", position=0)

    tree = _build(
        _node("a", evidence=ids),
        _node("a1", parent="a", evidence=ids),
        _node("b", evidence=ids),
        _node("c", evidence=ids),
        seeds=[seed],
        known=ids,
        limits=small,
    )

    # The seed and the three model roots make four; the deeper "a1" goes first, then nothing
    # else is needed.
    assert "Calvin cycle" in _titles(tree)
    assert "A1" not in _titles(tree)
    assert len(tree.nodes) == 3
    assert tree.dropped_over_limit == 2


def test_more_user_topics_than_the_limit_is_still_an_error() -> None:
    small = TopicLimits(
        max_topics=2,
        max_depth=3,
        max_title_chars=60,
        max_summary_chars=100,
        max_keywords_per_topic=4,
        max_keyword_chars=20,
        max_evidence_per_topic=3,
    )
    seeds = [TopicSeed(ref=f"s{i}", title=f"Topic {i}", position=i) for i in range(3)]

    with pytest.raises(TopicLimitError) as excinfo:
        _build(seeds=seeds, limits=small)
    assert (excinfo.value.count, excinfo.value.limit) == (3, 2)


def test_dropped_topics_do_not_count_towards_the_limit() -> None:
    ids = _ids(1)
    small = TopicLimits(3, 3, 60, 100, 4, 20, 3)

    tree = _build(
        *[_node(f"n{i}", evidence=ids) for i in range(3)],
        _node("u1"),
        _node("u2"),
        known=ids,
        limits=small,
    )

    assert len(tree.nodes) == 3
    assert tree.dropped_uncited == 2


def test_a_repeated_ref_is_rejected() -> None:
    ids = _ids(1)

    with pytest.raises(DuplicateRefError) as excinfo:
        _build(_node("a", evidence=ids), _node("a", evidence=ids), known=ids)

    assert excinfo.value.ref == "a"


def test_a_blank_ref_is_rejected() -> None:
    with pytest.raises(InvalidTopicError, match="ref"):
        _build(_node(" ", "T", evidence=_ids(1)), known=_ids(1))


def test_a_parent_that_does_not_exist_is_rejected() -> None:
    ids = _ids(1)

    with pytest.raises(UnknownParentError) as excinfo:
        _build(_node("a", parent="ghost", evidence=ids), known=ids)

    assert (excinfo.value.child, excinfo.value.parent) == ("a", "ghost")


def test_a_topic_that_is_its_own_parent_is_a_cycle() -> None:
    ids = _ids(1)

    with pytest.raises(CyclicHierarchyError):
        _build(_node("a", parent="a", evidence=ids), known=ids)


def test_a_two_topic_cycle_is_rejected() -> None:
    ids = _ids(1)

    with pytest.raises(CyclicHierarchyError):
        _build(
            _node("a", parent="b", evidence=ids),
            _node("b", parent="a", evidence=ids),
            known=ids,
        )


def test_a_cycle_that_does_not_include_the_first_node_is_still_found() -> None:
    ids = _ids(1)

    with pytest.raises(CyclicHierarchyError):
        _build(
            _node("root", evidence=ids),
            _node("x", parent="z", evidence=ids),
            _node("y", parent="x", evidence=ids),
            _node("z", parent="y", evidence=ids),
            known=ids,
        )


def test_a_cycle_is_rejected_even_when_every_member_would_have_been_dropped() -> None:
    """The structure is checked before anything is filtered: a model that
    produced a cycle is malformed however it cited.
    """
    with pytest.raises(CyclicHierarchyError):
        _build(_node("a", parent="b"), _node("b", parent="a"), known=_ids(1))


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_parent_ref_means_no_parent(blank: str) -> None:
    ids = _ids(1)

    tree = _build(_node("a", parent=blank, evidence=ids), known=ids)

    assert tree.nodes[0].parent_index is None
    assert tree.nodes[0].depth == 0


# --- text the model wrote -----------------------------------------------------------------


def test_a_title_is_normalised() -> None:
    ids = _ids(1)

    tree = _build(_node("a", "  Light \n reactions ＆ ATP ", evidence=ids), known=ids)

    assert tree.nodes[0].title == "Light reactions & ATP"


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_blank_title_is_rejected(blank: str) -> None:
    ids = _ids(1)

    with pytest.raises(InvalidTopicError, match="blank"):
        _build(_node("a", blank, evidence=ids), known=ids)


def test_an_over_long_title_is_rejected_and_one_at_the_limit_is_kept() -> None:
    ids = _ids(1)

    assert _build(_node("a", "x" * 60, evidence=ids), known=ids).nodes[0].title == "x" * 60
    with pytest.raises(InvalidTopicError, match="limit is 60"):
        _build(_node("a", "x" * 61, evidence=ids), known=ids)


def test_a_missing_or_blank_summary_becomes_none() -> None:
    ids = _ids(1)

    assert _build(_node("a", evidence=ids), known=ids).nodes[0].summary is None
    assert _build(_node("a", summary="  \n ", evidence=ids), known=ids).nodes[0].summary is None


def test_a_summary_is_normalised() -> None:
    ids = _ids(1)

    tree = _build(_node("a", summary="Plants  make\nfood.", evidence=ids), known=ids)

    assert tree.nodes[0].summary == "Plants make food."


def test_an_over_long_summary_is_trimmed_at_a_word_boundary_not_rejected() -> None:
    ids = _ids(1)
    summary = "alpha beta gamma " * 20

    tree = _build(_node("a", summary=summary, evidence=ids), known=ids)

    trimmed = tree.nodes[0].summary
    assert trimmed is not None
    assert len(trimmed) <= 100
    assert trimmed.endswith("…")
    body = trimmed[:-1]
    assert summary.startswith(body)
    assert summary[len(body)] == " "  # cut between words, not through one


def test_a_summary_exactly_at_the_limit_is_left_alone() -> None:
    ids = _ids(1)

    tree = _build(_node("a", summary="s" * 100, evidence=ids), known=ids)

    assert tree.nodes[0].summary == "s" * 100


def test_a_topics_keywords_are_tidied_leniently() -> None:
    ids = _ids(1)
    raw = ["Chlorophyll", "chlorophyll", " ", "x" * 21, "Light", "ATP", "NADPH", "extra"]

    tree = _build(_node("a", keywords=raw, evidence=ids), known=ids)

    # duplicate, blank and over-long terms dropped; capped at four; order kept
    assert tree.nodes[0].keywords == ("Chlorophyll", "Light", "ATP", "NADPH")


# --- validate_and_build: keywords only -----------------------------------------------------


SEED_A = TopicSeed(ref="seed-a", title="Photosynthesis", position=0)
SEED_B = TopicSeed(ref="seed-b", title="Respiration", position=1)


def test_keywords_alone_with_an_empty_draft_yield_the_seeds_as_bare_roots() -> None:
    tree = _build(seeds=[SEED_A, SEED_B])

    assert tree.mode == "keywords"
    assert _titles(tree) == ["Photosynthesis", "Respiration"]
    assert [n.origin for n in tree.nodes] == ["user", "user"]
    assert [n.seed_ref for n in tree.nodes] == ["seed-a", "seed-b"]
    assert [n.position for n in tree.nodes] == [0, 1]
    assert all(n.depth == 0 and n.parent_index is None for n in tree.nodes)
    assert all(n.evidence_chunk_ids == () and n.summary is None for n in tree.nodes)
    assert tree.dropped_uncited == 0


def test_without_material_the_models_additions_are_suggested_and_carry_no_evidence() -> None:
    tree = _build(
        _node("t1", "Photosynthesis", seed="seed-a", summary="How plants make food."),
        _node("t2", "Light reactions", parent="t1", evidence=[uuid4()]),  # invented citation
        _node("t3", "Calvin cycle", parent="t1"),
        seeds=[SEED_A, SEED_B],
    )

    assert _titles(tree) == ["Photosynthesis", "Light reactions", "Calvin cycle", "Respiration"]
    seed_node, light, calvin, respiration = tree.nodes
    assert seed_node.origin == "user"
    assert seed_node.summary == "How plants make food."
    assert (light.origin, light.evidence_chunk_ids) == ("suggested", ())
    assert (calvin.origin, calvin.evidence_chunk_ids) == ("suggested", ())
    assert (light.depth, light.position, light.parent_index) == (1, 0, 0)
    assert (calvin.depth, calvin.position) == (1, 1)
    assert respiration.origin == "user"  # never mentioned by the model, still kept


def test_without_material_nothing_is_ever_dropped_for_being_uncited() -> None:
    tree = _build(_node("t", "A suggestion"), seeds=[SEED_A])

    assert tree.dropped_uncited == 0
    assert _titles(tree) == ["Photosynthesis", "A suggestion"]


def test_a_model_root_without_material_is_suggested_and_placed_after_the_seeds() -> None:
    tree = _build(_node("t", "Extra idea"), seeds=[SEED_A, SEED_B])

    extra = _by_title(tree, "Extra idea")
    assert extra.origin == "suggested"
    assert extra.position == 2  # one past the highest seed position
    assert extra.seed_ref is None


def test_seeds_come_first_in_the_order_given_whatever_order_the_model_used() -> None:
    tree = _build(
        _node("t2", "Respiration", seed="seed-b"),
        _node("t1", "Photosynthesis", seed="seed-a"),
        seeds=[SEED_A, SEED_B],
    )

    assert _titles(tree) == ["Photosynthesis", "Respiration"]


def test_a_seed_keeps_its_existing_position_even_when_positions_have_gaps() -> None:
    gappy = [TopicSeed("s0", "Zero", 0), TopicSeed("s3", "Three", 3)]

    tree = _build(_node("x", "New"), seeds=gappy)

    assert [n.position for n in tree.nodes] == [0, 3, 4]  # the new root goes after position 3


def test_an_explicit_first_root_position_is_honoured_when_it_clears_every_seed() -> None:
    tree = _build(_node("x", "New"), seeds=[SEED_A, SEED_B], first_root_position=10)

    assert _by_title(tree, "New").position == 10


def test_a_first_root_position_that_collides_with_a_seed_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="first_root_position"):
        _build(_node("x", "New"), seeds=[SEED_A, SEED_B], first_root_position=1)


def test_the_same_seed_ref_supplied_twice_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="more than once"):
        _build(seeds=[SEED_A, SEED_A])


# --- validate_and_build: PDF + keywords ---------------------------------------------------


def test_a_covered_seed_keeps_the_users_wording_gains_evidence_and_is_still_a_user_topic() -> None:
    ids = _ids(2)

    tree = _build(
        _node(
            "t1",
            "Photosynthesis (the model's spelling)",
            seed="seed-a",
            summary="From the material.",
            keywords=["chlorophyll"],
            evidence=[ids[0]],
        ),
        _node("t2", "Light reactions", parent="t1", evidence=[ids[1]]),
        seeds=[TopicSeed("seed-a", "photosynthesis", 0)],
        known=ids,
    )

    assert tree.mode == "pdf_keywords"
    seed_node, child = tree.nodes
    assert seed_node.title == "photosynthesis"  # verbatim, not the model's title
    assert seed_node.origin == "user"
    assert seed_node.seed_ref == "seed-a"
    assert seed_node.evidence_chunk_ids == (ids[0],)
    assert seed_node.summary == "From the material."
    assert seed_node.keywords == ("chlorophyll",)
    assert (child.origin, child.depth, child.parent_index) == ("source", 1, 0)


def test_a_seed_the_material_does_not_cover_survives_with_no_evidence() -> None:
    ids = _ids(1)

    tree = _build(
        _node("t1", "Photosynthesis", seed="seed-a", evidence=ids),
        _node("t2", "Respiration", seed="seed-b"),  # linked, but cites nothing
        seeds=[SEED_A, SEED_B],
        known=ids,
    )

    uncovered = _by_title(tree, "Respiration")
    assert uncovered.origin == "user"
    assert uncovered.evidence_chunk_ids == ()
    assert tree.dropped_uncited == 0


def test_a_seed_the_model_omitted_is_added_as_a_bare_root() -> None:
    ids = _ids(1)

    tree = _build(
        _node("t1", "Photosynthesis", seed="seed-a", evidence=ids),
        seeds=[SEED_A, SEED_B],
        known=ids,
    )

    assert _titles(tree) == ["Photosynthesis", "Respiration"]
    omitted = _by_title(tree, "Respiration")
    assert (omitted.origin, omitted.seed_ref, omitted.evidence_chunk_ids) == (
        "user",
        "seed-b",
        (),
    )
    assert omitted.summary is None and omitted.keywords == ()


def test_a_node_titled_like_a_seed_is_taken_to_be_that_seed_when_the_link_is_missing() -> None:
    ids = _ids(1)

    tree = _build(_node("t1", "  PHOTOSYNTHESIS ", evidence=ids), seeds=[SEED_A], known=ids)

    assert len(tree.nodes) == 1
    assert tree.nodes[0].title == "Photosynthesis"  # the user's wording wins
    assert (tree.nodes[0].origin, tree.nodes[0].seed_ref) == ("user", "seed-a")
    assert tree.nodes[0].evidence_chunk_ids == (ids[0],)


def test_an_explicit_link_wins_over_a_title_match() -> None:
    ids = _ids(1)

    tree = _build(
        # Linked to seed-a although it is titled like seed-b.
        _node("n1", "Respiration", seed="seed-a", evidence=ids),
        # Titled like seed-a, but seed-a is already claimed: an ordinary topic.
        _node("n2", "Photosynthesis", evidence=ids),
        seeds=[SEED_A, SEED_B],
        known=ids,
    )

    assert [(n.title, n.origin, n.seed_ref) for n in tree.nodes] == [
        ("Photosynthesis", "user", "seed-a"),  # n1, carrying the user's wording for seed-a
        ("Respiration", "user", "seed-b"),  # seed-b, unclaimed: added bare
        ("Photosynthesis", "source", None),  # n2, a new topic
    ]


def test_one_seed_is_claimed_by_at_most_one_node() -> None:
    ids = _ids(1)

    tree = _build(
        _node("n1", "First", seed="seed-a", evidence=ids),
        _node("n2", "Second", seed="seed-a"),  # a second claim on seed-a, citing nothing
        seeds=[SEED_A],
        known=ids,
    )

    assert [n.seed_ref for n in tree.nodes] == ["seed-a"]
    assert tree.dropped_uncited == 1  # n2 became an ordinary, uncited topic


def test_a_link_to_a_seed_that_does_not_exist_is_ignored() -> None:
    ids = _ids(1)

    tree = _build(
        _node("n1", "Real", seed="ghost", evidence=ids),
        _node("n2", "Uncited", seed="ghost"),
        seeds=[SEED_A],
        known=ids,
    )

    real = _by_title(tree, "Real")
    assert (real.origin, real.seed_ref) == ("source", None)
    assert "Uncited" not in _titles(tree)
    assert tree.dropped_uncited == 1


def test_a_seed_is_always_a_root_whatever_parent_the_model_gave_it() -> None:
    ids = _ids(1)

    tree = _build(
        _node("other", "Other", evidence=ids),
        _node("s", "Photosynthesis", parent="other", seed="seed-a", evidence=ids),
        seeds=[SEED_A],
        known=ids,
    )

    seed_node = _by_title(tree, "Photosynthesis")
    assert seed_node.parent_index is None
    assert seed_node.depth == 0


def test_making_a_seed_the_child_of_its_own_child_is_not_a_cycle() -> None:
    """The model's parent for a seed is discarded, which is exactly what
    dissolves this apparent loop: the seed stays a root and the other node is
    simply its child.
    """
    ids = _ids(1)

    tree = _build(
        _node("s", "Photosynthesis", parent="c", seed="seed-a", evidence=ids),
        _node("c", "Light reactions", parent="s", evidence=ids),
        seeds=[SEED_A],
        known=ids,
    )

    assert _titles(tree) == ["Photosynthesis", "Light reactions"]
    assert tree.nodes[1].parent_index == 0


def test_uncited_proposals_are_dropped_but_seeds_never_are() -> None:
    ids = _ids(1)

    tree = _build(
        _node("keep", "Cited", evidence=ids),
        _node("drop1", "Uncited one"),
        _node("drop2", "Uncited two", parent="keep"),
        seeds=[SEED_A],
        known=ids,
    )

    assert tree.dropped_uncited == 2
    assert _titles(tree) == ["Photosynthesis", "Cited"]


def test_a_seeds_subtopics_are_numbered_from_zero_beneath_it() -> None:
    ids = _ids(1)

    tree = _build(
        _node("s", "Photosynthesis", seed="seed-a", evidence=ids),
        _node("c1", "One", parent="s", evidence=ids),
        _node("c2", "Two", parent="s", evidence=ids),
        seeds=[SEED_A],
        known=ids,
    )

    children = [(n.title, n.depth, n.position) for n in tree.nodes[1:]]
    assert children == [("One", 1, 0), ("Two", 1, 1)]


def test_new_model_roots_follow_the_seeds_in_the_pdf_plus_keywords_mode() -> None:
    ids = _ids(1)

    tree = _build(
        _node("x", "Model topic", evidence=ids),
        _node("s", "Photosynthesis", seed="seed-a", evidence=ids),
        seeds=[SEED_A, SEED_B],
        known=ids,
    )

    assert [(n.title, n.position) for n in tree.nodes] == [
        ("Photosynthesis", 0),
        ("Respiration", 1),
        ("Model topic", 2),
    ]


def test_a_seeds_title_is_never_rewritten_even_if_it_would_fail_the_title_rules() -> None:
    """The title limit is for what the *model* wrote. The user's own wording
    was validated when it was typed and is stored as given.
    """
    long_seed = TopicSeed("seed-long", "y" * 200, 0)

    tree = _build(seeds=[long_seed])

    assert tree.nodes[0].title == "y" * 200


# --- determinism & invariants -------------------------------------------------------------------


def test_building_twice_from_the_same_input_gives_the_same_tree() -> None:
    ids = _ids(3)
    nodes = (
        _node("a", "A", evidence=ids[:1], keywords=["k"]),
        _node("b", "B", parent="a", evidence=ids[1:2], summary="Summary."),
        _node("c", "C", evidence=ids[2:]),
    )

    first = _build(*nodes, seeds=[SEED_A], known=ids)
    second = _build(*nodes, seeds=[SEED_A], known=ids)

    assert first == second


def _as_rows(tree: ValidatedTopicTree) -> list[Row]:
    return [
        Row(
            id=_uuid(node.index + 1),
            parent_id=None if node.parent_index is None else _uuid(node.parent_index + 1),
            depth=node.depth,
            position=node.position,
        )
        for node in tree.nodes
    ]


def _random_case(rng: random.Random):
    known = [UUID(int=rng.getrandbits(128)) for _ in range(rng.randint(0, 6))]
    seed_count = rng.randint(0, 4) if known else rng.randint(1, 4)
    position = 0
    seeds: list[TopicSeed] = []
    for i in range(seed_count):
        position += rng.randint(0, 2)
        seeds.append(TopicSeed(ref=f"seed-{i}", title=f"Seed {i}", position=position))
        position += 1

    count = rng.randint(0, 12)
    order = list(range(count))
    rng.shuffle(order)  # rank decides who may be whose parent, keeping it acyclic
    rank = {i: r for r, i in enumerate(order)}
    nodes: list[TopicNodeDraft] = []
    for i in range(count):
        earlier = [j for j in range(count) if rank[j] < rank[i]]
        parent = f"n{rng.choice(earlier)}" if earlier and rng.random() < 0.7 else None
        cited = [str(rng.choice(known)) for _ in range(rng.randint(0, 3))] if known else []
        if rng.random() < 0.2:
            cited.append(str(UUID(int=rng.getrandbits(128))))  # one it was never shown
        title = f"Topic {i}"
        if seeds and rng.random() < 0.25:
            title = rng.choice(seeds).title.upper()  # collides with a seed's title
        link = rng.choice([None, None, "bogus", *[s.ref for s in seeds]])
        nodes.append(
            TopicNodeDraft(
                ref=f"n{i}",
                title=title,
                parent_ref=parent,
                evidence_chunk_ids=tuple(cited),
                seed_ref=link,
            )
        )
    rng.shuffle(nodes)  # the draft order is arbitrary
    return nodes, seeds, known


@pytest.mark.parametrize("seed", range(10))
def test_invariants_hold_for_random_drafts(seed: int) -> None:
    rng = random.Random(seed)
    big = TopicLimits(200, 50, 60, 100, 4, 20, 3)
    for _ in range(80):
        nodes, seeds, known = _random_case(rng)
        try:
            tree = _build(*nodes, seeds=seeds, known=known, limits=big)
        except NoTopicsError:
            assert not seeds  # only possible when there was nothing to fall back on
            continue

        has_passages = bool(known)
        assert [n.index for n in tree.nodes] == list(range(len(tree.nodes)))
        for n in tree.nodes:
            # hierarchy: a parent precedes its child, and depth follows the chain
            assert n.parent_index is None or n.parent_index < n.index
            expected_depth = 0 if n.parent_index is None else tree.nodes[n.parent_index].depth + 1
            assert n.depth == expected_depth
            # evidence: only shown chunks, and the per-origin invariant always holds
            assert set(n.evidence_chunk_ids) <= set(known)
            assert evidence_count_is_valid(n.origin, len(n.evidence_chunk_ids))
            # origin is decided by the rules
            assert (n.origin == "user") == (n.seed_ref is not None)
            if n.seed_ref is None:
                assert n.origin == ("source" if has_passages else "suggested")
        # every user keyword survives exactly once, in the user's own words
        by_ref = {n.seed_ref: n for n in tree.nodes if n.seed_ref is not None}
        assert set(by_ref) == {s.ref for s in seeds}
        assert len([n for n in tree.nodes if n.seed_ref is not None]) == len(seeds)
        for s in seeds:
            assert by_ref[s.ref].title == s.title
            assert by_ref[s.ref].parent_index is None
            assert by_ref[s.ref].position == s.position
        # the built tree is a consistent stored tree, in reading order
        rows = _as_rows(tree)
        validate_hierarchy(rows)
        assert [r.id for r in reading_order(rows)] == [r.id for r in rows]


# --- rules over stored topics: validate_hierarchy --------------------------------------------


def _forest() -> list[Row]:
    return [
        Row(_uuid(1), None, 0, 0),
        Row(_uuid(2), None, 0, 1),
        Row(_uuid(3), _uuid(1), 1, 0),
        Row(_uuid(4), _uuid(1), 1, 1),
        Row(_uuid(5), _uuid(3), 2, 0),
    ]


def test_a_consistent_forest_validates() -> None:
    validate_hierarchy(_forest())
    validate_hierarchy(_forest(), max_depth=3)
    validate_hierarchy([])


def test_the_same_position_under_different_parents_is_fine() -> None:
    validate_hierarchy(
        [
            Row(_uuid(1), None, 0, 0),
            Row(_uuid(2), _uuid(1), 1, 0),
            Row(_uuid(3), _uuid(2), 2, 0),
        ]
    )


def test_a_missing_parent_is_rejected() -> None:
    with pytest.raises(UnknownParentError):
        validate_hierarchy([Row(_uuid(2), _uuid(99), 1, 0)])


def test_a_topic_that_is_its_own_parent_is_rejected() -> None:
    with pytest.raises(CyclicHierarchyError):
        validate_hierarchy([Row(_uuid(1), _uuid(1), 0, 0)])


def test_a_longer_cycle_among_stored_topics_is_rejected() -> None:
    with pytest.raises(CyclicHierarchyError):
        validate_hierarchy(
            [
                Row(_uuid(1), _uuid(3), 1, 0),
                Row(_uuid(2), _uuid(1), 1, 0),
                Row(_uuid(3), _uuid(2), 1, 0),
            ]
        )


def test_a_roots_depth_must_be_zero() -> None:
    with pytest.raises(TopicDepthError):
        validate_hierarchy([Row(_uuid(1), None, 1, 0)])


def test_a_childs_depth_must_be_its_parents_plus_one() -> None:
    rows = [Row(_uuid(1), None, 0, 0), Row(_uuid(2), _uuid(1), 5, 0)]

    with pytest.raises(TopicDepthError, match="makes that 1"):
        validate_hierarchy(rows)


def test_depth_is_checked_against_the_limit_when_one_is_given() -> None:
    with pytest.raises(TopicDepthError, match="at most 2 levels"):
        validate_hierarchy(_forest(), max_depth=2)  # the deepest topic is at depth 2
    validate_hierarchy(_forest(), max_depth=3)


def test_a_negative_position_is_rejected() -> None:
    with pytest.raises(TopicOrderError, match="negative"):
        validate_hierarchy([Row(_uuid(1), None, 0, -1)])


def test_two_siblings_may_not_share_a_position() -> None:
    with pytest.raises(TopicOrderError, match="share position"):
        validate_hierarchy(
            [
                Row(_uuid(1), None, 0, 0),
                Row(_uuid(2), _uuid(1), 1, 0),
                Row(_uuid(3), _uuid(1), 1, 0),
            ]
        )


def test_two_roots_may_not_share_a_position() -> None:
    with pytest.raises(TopicOrderError):
        validate_hierarchy([Row(_uuid(1), None, 0, 2), Row(_uuid(2), None, 0, 2)])


def test_validation_only_validates_it_never_rewrites_depth() -> None:
    """Depth is caller-supplied. A wrong value is reported, not silently
    corrected - and the rows the caller passed are untouched.
    """
    wrong = [Row(_uuid(1), None, 0, 0), Row(_uuid(2), _uuid(1), 7, 0)]
    snapshot = list(wrong)

    with pytest.raises(TopicDepthError):
        validate_hierarchy(wrong)

    assert wrong == snapshot
    assert wrong[1].depth == 7


# --- reading_order --------------------------------------------------------------------------


def test_reading_order_is_depth_first_by_position() -> None:
    forest = _forest()

    ordered = reading_order(forest)

    assert [r.id for r in ordered] == [_uuid(1), _uuid(3), _uuid(5), _uuid(4), _uuid(2)]


def test_reading_order_ignores_the_order_the_rows_arrive_in() -> None:
    forest = _forest()
    shuffled = [forest[i] for i in (4, 1, 3, 0, 2)]

    assert reading_order(shuffled) == reading_order(forest)


def test_reading_order_sorts_siblings_by_position_not_by_id() -> None:
    rows = [
        Row(_uuid(1), None, 0, 2),
        Row(_uuid(2), None, 0, 0),
        Row(_uuid(3), None, 0, 1),
    ]

    assert [r.id for r in reading_order(rows)] == [_uuid(2), _uuid(3), _uuid(1)]


def test_reading_order_breaks_position_ties_by_id_so_it_is_stable() -> None:
    rows = [Row(_uuid(9), None, 0, 0), Row(_uuid(4), None, 0, 0)]

    assert [r.id for r in reading_order(rows)] == [_uuid(4), _uuid(9)]


def test_reading_order_returns_the_same_objects_and_a_new_list() -> None:
    forest = _forest()

    ordered = reading_order(forest)

    assert ordered is not forest
    assert all(any(item is original for original in forest) for item in ordered)
    assert len(ordered) == len(forest)


def test_reading_order_of_nothing_is_nothing() -> None:
    assert reading_order([]) == []


def test_reading_order_rejects_a_missing_parent_and_a_cycle() -> None:
    with pytest.raises(UnknownParentError):
        reading_order([Row(_uuid(2), _uuid(99), 1, 0)])
    with pytest.raises(CyclicHierarchyError):
        reading_order([Row(_uuid(1), _uuid(2), 0, 0), Row(_uuid(2), _uuid(1), 0, 0)])


def test_reading_order_does_not_recompute_or_require_consistent_depth() -> None:
    rows = [Row(_uuid(1), None, 9, 0), Row(_uuid(2), _uuid(1), 0, 0)]

    ordered = reading_order(rows)

    assert [(r.id, r.depth) for r in ordered] == [(_uuid(1), 9), (_uuid(2), 0)]


def test_reading_order_copes_with_a_deep_chain_without_recursion_limits() -> None:
    rows = [Row(_uuid(1), None, 0, 0)] + [
        Row(_uuid(n), _uuid(n - 1), n - 1, 0) for n in range(2, 3001)
    ]

    ordered = reading_order(rows)

    assert [r.id for r in ordered] == [_uuid(n) for n in range(1, 3001)]


# --- accepted_topics ----------------------------------------------------------------------------


def _with_status(rows: list[Row], **statuses: str) -> list[Row]:
    """``rows`` with statuses set by keyword: ``t3="accepted"`` targets the row
    whose id is ``_uuid(3)``. Rows not named keep their status.
    """
    return [
        Row(
            row.id,
            row.parent_id,
            row.depth,
            row.position,
            statuses.get(f"t{row.id.int}", row.status),
        )
        for row in rows
    ]


def test_only_accepted_topics_are_returned_in_reading_order() -> None:
    rows = _with_status(_forest(), t1="accepted", t2="rejected", t3="accepted", t4="proposed")

    assert [r.id for r in accepted_topics(rows)] == [_uuid(1), _uuid(3)]


def test_a_rejected_parent_takes_its_whole_subtree_out_even_if_the_children_are_accepted() -> None:
    rows = _with_status(
        _forest(), t1="rejected", t2="accepted", t3="accepted", t4="accepted", t5="accepted"
    )

    assert [r.id for r in accepted_topics(rows)] == [_uuid(2)]


def test_a_parent_that_is_only_proposed_also_excludes_its_subtree() -> None:
    rows = _with_status(
        _forest(), t1="proposed", t2="accepted", t3="accepted", t4="accepted", t5="accepted"
    )

    assert [r.id for r in accepted_topics(rows)] == [_uuid(2)]


def test_an_accepted_chain_is_returned_whole() -> None:
    rows = _with_status(
        _forest(), t1="accepted", t2="accepted", t3="accepted", t4="accepted", t5="accepted"
    )

    assert [r.id for r in accepted_topics(rows)] == [
        _uuid(1),
        _uuid(3),
        _uuid(5),
        _uuid(4),
        _uuid(2),
    ]


def test_a_rejected_middle_topic_removes_only_its_own_descendants() -> None:
    rows = _with_status(
        _forest(), t1="accepted", t2="accepted", t3="rejected", t4="accepted", t5="accepted"
    )

    assert [r.id for r in accepted_topics(rows)] == [_uuid(1), _uuid(4), _uuid(2)]


def test_nothing_accepted_yields_nothing() -> None:
    assert accepted_topics(_forest()) == []
    assert accepted_topics([]) == []


def test_accepted_topics_propagates_a_broken_hierarchy() -> None:
    with pytest.raises(CyclicHierarchyError):
        accepted_topics([Row(_uuid(1), _uuid(1), 0, 0, "accepted")])
