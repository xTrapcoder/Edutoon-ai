"""``domain.script_duration`` - pure duration allocation/splitting maths (Phase 7).

No I/O, no provider, no database: every case here is deterministic arithmetic.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.domain.script_duration import (
    allocate_topic_budgets,
    max_provider_calls,
    split_topic_budget,
)

# --- allocate_topic_budgets --------------------------------------------------------


def test_single_topic_gets_the_whole_target() -> None:
    topic = uuid4()
    (budget,) = allocate_topic_budgets(
        topic_ids=[topic],
        claim_counts={topic: 3},
        target_duration_ms=300_000,
        min_section_duration_ms=30_000,
    )
    assert budget.topic_id == topic
    assert budget.duration_ms == 300_000


def test_equal_evidence_splits_the_remainder_equally() -> None:
    topics = [uuid4(), uuid4(), uuid4()]
    budgets = allocate_topic_budgets(
        topic_ids=topics,
        claim_counts={t: 2 for t in topics},
        target_duration_ms=300_000,
        min_section_duration_ms=30_000,
    )
    # floor 30_000 each (90_000 total), remainder 210_000 split evenly -> 100_000 each
    assert [b.duration_ms for b in budgets] == [100_000, 100_000, 100_000]


def test_more_evidence_gets_a_larger_share() -> None:
    heavy, light = uuid4(), uuid4()
    budgets = allocate_topic_budgets(
        topic_ids=[heavy, light],
        claim_counts={heavy: 3, light: 1},
        target_duration_ms=100_000,
        min_section_duration_ms=10_000,
    )
    by_id = {b.topic_id: b.duration_ms for b in budgets}
    assert by_id[heavy] > by_id[light]


def test_allocation_always_sums_exactly_to_the_target() -> None:
    # An odd target that does not divide evenly across topics, to exercise the
    # leftover-distribution loop.
    topics = [uuid4() for _ in range(7)]
    budgets = allocate_topic_budgets(
        topic_ids=topics,
        claim_counts={t: i + 1 for i, t in enumerate(topics)},
        target_duration_ms=1_000_003,
        min_section_duration_ms=30_000,
    )
    assert sum(b.duration_ms for b in budgets) == 1_000_003


def test_a_topic_missing_from_claim_counts_still_gets_a_fair_share() -> None:
    known, unknown = uuid4(), uuid4()
    budgets = allocate_topic_budgets(
        topic_ids=[known, unknown],
        claim_counts={known: 5},  # unknown has no entry at all
        target_duration_ms=200_000,
        min_section_duration_ms=10_000,
    )
    by_id = {b.topic_id: b.duration_ms for b in budgets}
    # unknown is treated as weight 1, known as weight 5: not starved to the floor.
    assert by_id[unknown] > 10_000


def test_too_many_topics_for_the_target_raises() -> None:
    topics = [uuid4() for _ in range(5)]
    with pytest.raises(ValueError, match="cannot each receive"):
        allocate_topic_budgets(
            topic_ids=topics,
            claim_counts={t: 1 for t in topics},
            target_duration_ms=100_000,
            min_section_duration_ms=30_000,
        )


def test_empty_topic_list_raises() -> None:
    with pytest.raises(ValueError, match="at least one topic"):
        allocate_topic_budgets(
            topic_ids=[],
            claim_counts={},
            target_duration_ms=100_000,
            min_section_duration_ms=30_000,
        )


def test_non_positive_floor_raises() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        allocate_topic_budgets(
            topic_ids=[uuid4()],
            claim_counts={},
            target_duration_ms=100_000,
            min_section_duration_ms=0,
        )


def test_allocation_is_deterministic() -> None:
    topics = [uuid4() for _ in range(4)]
    counts = {t: i + 1 for i, t in enumerate(topics)}
    first = allocate_topic_budgets(
        topic_ids=topics,
        claim_counts=counts,
        target_duration_ms=777_777,
        min_section_duration_ms=30_000,
    )
    second = allocate_topic_budgets(
        topic_ids=topics,
        claim_counts=counts,
        target_duration_ms=777_777,
        min_section_duration_ms=30_000,
    )
    assert first == second


# --- split_topic_budget -------------------------------------------------------------


def test_a_budget_within_the_cap_is_not_split() -> None:
    assert split_topic_budget(250_000, max_section_duration_ms=300_000) == (250_000,)


def test_a_budget_at_exactly_the_cap_is_not_split() -> None:
    assert split_topic_budget(300_000, max_section_duration_ms=300_000) == (300_000,)


def test_an_over_cap_budget_splits_into_even_chunks() -> None:
    chunks = split_topic_budget(700_000, max_section_duration_ms=300_000)
    assert len(chunks) == 3
    assert sum(chunks) == 700_000
    assert max(chunks) - min(chunks) <= 1


def test_a_very_long_budget_splits_into_many_bounded_chunks() -> None:
    chunks = split_topic_budget(3_600_000, max_section_duration_ms=300_000)
    assert len(chunks) == 12
    assert sum(chunks) == 3_600_000
    assert all(chunk <= 300_000 for chunk in chunks)


@pytest.mark.parametrize(
    "duration_ms,max_section_duration_ms", [(0, 100), (-1, 100), (100, 0), (100, -1)]
)
def test_non_positive_arguments_raise(duration_ms: int, max_section_duration_ms: int) -> None:
    with pytest.raises(ValueError):
        split_topic_budget(duration_ms, max_section_duration_ms=max_section_duration_ms)


# --- max_provider_calls --------------------------------------------------------------


def test_max_provider_calls_matches_the_defaults() -> None:
    # D/M + n = 3_600_000/300_000 + 20 = 12 + 20 = 32 sections; 1 outline + 2*32.
    assert (
        max_provider_calls(
            max_target_duration_ms=3_600_000,
            max_section_duration_ms=300_000,
            max_script_sections=20,
        )
        == 65
    )


def test_max_provider_calls_is_an_upper_bound_on_a_real_allocation() -> None:
    max_target = 3_600_000
    max_section = 300_000
    max_sections = 20
    budget = max_provider_calls(
        max_target_duration_ms=max_target,
        max_section_duration_ms=max_section,
        max_script_sections=max_sections,
    )

    # A pathological real allocation: as many topics as allowed, each getting
    # just over a multiple of the per-call cap so every one needs an extra chunk.
    topics = [uuid4() for _ in range(max_sections)]
    budgets = allocate_topic_budgets(
        topic_ids=topics,
        claim_counts={t: 1 for t in topics},
        target_duration_ms=max_target,
        min_section_duration_ms=30_000,
    )
    total_calls = sum(
        len(split_topic_budget(b.duration_ms, max_section_duration_ms=max_section)) for b in budgets
    )
    # 1 outline + total_calls (first pass) + total_calls (bounded correction pass)
    assert 1 + 2 * total_calls <= budget
