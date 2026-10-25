"""``domain/claim_extraction.py`` - pure candidate-claim rules (Phase 6).

No I/O, no database: every case here is a plain function call.
"""

from __future__ import annotations

import pytest

from edutoon.domain.claim_extraction import (
    ClaimCandidateDraft,
    ClaimCandidateLimits,
    ClaimExtractionDraft,
    validate_candidates,
)

LIMITS = ClaimCandidateLimits(max_claims_per_topic=3, max_claim_text_chars=40)


def _draft(*texts: str) -> ClaimExtractionDraft:
    return ClaimExtractionDraft(candidates=tuple(ClaimCandidateDraft(text=t) for t in texts))


# --- ClaimCandidateLimits ------------------------------------------------------------


@pytest.mark.parametrize("field", ["max_claims_per_topic", "max_claim_text_chars"])
def test_a_limit_below_one_is_rejected(field: str) -> None:
    kwargs = {"max_claims_per_topic": 3, "max_claim_text_chars": 40, field: 0}
    with pytest.raises(ValueError, match=field):
        ClaimCandidateLimits(**kwargs)


# --- validate_candidates: basic cleaning ----------------------------------------------


def test_a_well_formed_candidate_survives_unchanged() -> None:
    result = validate_candidates(_draft("Plants absorb carbon dioxide."), limits=LIMITS)

    assert result == ["Plants absorb carbon dioxide."]


def test_leading_and_trailing_whitespace_and_repeated_internal_whitespace_are_collapsed() -> None:
    result = validate_candidates(_draft("  Plants   absorb  CO2.  "), limits=LIMITS)

    assert result == ["Plants absorb CO2."]


def test_a_blank_candidate_is_dropped() -> None:
    result = validate_candidates(_draft("   ", "A real claim."), limits=LIMITS)

    assert result == ["A real claim."]


def test_an_over_long_candidate_is_dropped_rather_than_truncated() -> None:
    too_long = "x" * (LIMITS.max_claim_text_chars + 1)

    result = validate_candidates(_draft(too_long, "A short one."), limits=LIMITS)

    assert result == ["A short one."]


def test_a_candidate_at_exactly_the_length_limit_survives() -> None:
    exact = "x" * LIMITS.max_claim_text_chars

    result = validate_candidates(_draft(exact), limits=LIMITS)

    assert result == [exact]


# --- validate_candidates: de-duplication ----------------------------------------------


def test_duplicate_candidates_collapse_to_the_first() -> None:
    result = validate_candidates(_draft("A fact.", "A fact.", "a fact."), limits=LIMITS)

    assert result == ["A fact."]


def test_a_candidate_matching_an_existing_claim_text_is_dropped() -> None:
    result = validate_candidates(
        _draft("A fact.", "A new fact."),
        limits=LIMITS,
        existing_claim_texts=["A fact."],
    )

    assert result == ["A new fact."]


def test_matching_an_existing_claim_text_is_case_and_whitespace_insensitive() -> None:
    result = validate_candidates(
        _draft("  a   FACT.  "), limits=LIMITS, existing_claim_texts=["A fact."]
    )

    assert result == []


def test_every_candidate_already_extracted_leaves_an_empty_result_not_an_error() -> None:
    result = validate_candidates(
        _draft("A fact.", "Another fact."),
        limits=LIMITS,
        existing_claim_texts=["A fact.", "Another fact."],
    )

    assert result == []


def test_no_candidates_at_all_leaves_an_empty_result_not_an_error() -> None:
    result = validate_candidates(_draft(), limits=LIMITS)

    assert result == []


# --- validate_candidates: the cap ------------------------------------------------------


def test_the_result_is_capped_at_max_claims_per_topic_keeping_model_order() -> None:
    result = validate_candidates(_draft("One.", "Two.", "Three.", "Four."), limits=LIMITS)

    assert result == ["One.", "Two.", "Three."]


def test_dropped_candidates_do_not_count_against_the_cap() -> None:
    result = validate_candidates(_draft("", "One.", "one.", "Two.", "Three."), limits=LIMITS)

    assert result == ["One.", "Two.", "Three."]
