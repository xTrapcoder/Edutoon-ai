"""Citation relevance: whether a cited claim is *about* the sentence that cites it.

Without ``claim_texts``, ``filter_and_validate`` accepts a citation when the id is a supported
claim and a segment keeps at least one (or is short and marked connective); with a
``GroundingReference`` it also requires every figure and name in the narration to appear somewhere
in the topic's claims. It never compares a sentence with the claim it cites; the first half of this
file pins that old behaviour. With ``claim_texts`` a cited claim sharing no content word with its
segment is dropped, which is a floor and not a proof (the second half). The example is real:
segment 29 of the attempt-6 script (job 01a14430) was rewritten after "lists no claim ids" and
came back citing two claims about regulators and advisers for a hand-off about fixed
income securities.

See ``docs/citation-relevance-proposal.md``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.domain.script import ScriptDraft, ScriptSegmentDraft
from edutoon.domain.script_provenance import (
    WEAK_CITATION_COVERAGE,
    GroundingReference,
    UnsourcedSegmentError,
    filter_and_validate,
    relevant_claim_ids,
    weak_citation_coverage,
)
from edutoon.domain.storyboard_provenance import has_lexical_overlap

WPM = 150
TOPIC = uuid4()
REGULATORS = uuid4()
ADVISERS = uuid4()
BONDS = uuid4()

CLAIM_TEXTS = {
    REGULATORS: "Regulatory bodies ensure fair trading practices and investor protection.",
    ADVISERS: "Investment Advisers provide tailored financial guidance and match goals.",
    BONDS: "Fixed income securities offer regular interest payments and capital preservation.",
}
GROUNDING = GroundingReference(
    topic_texts={TOPIC: "Channels for Making Investments " + " ".join(CLAIM_TEXTS.values())},
    frame_text="Investments Channels for Making Investments",
)

HAND_OFF = (
    "In the next section, we will explore fixed income securities, which are investments that "
    "provide regular interest payments and the promise of capital preservation."
)


def _validate(narration: str, cited: list[object], *, claim_texts=None):
    segment = ScriptSegmentDraft(
        segment_index=0,
        narration_text=narration,
        claim_ids=[str(claim_id) for claim_id in cited],
        estimated_duration_ms=0,
        connective=False,
        topic_id=TOPIC,
    )
    draft = ScriptDraft(title="T", segments=[segment], estimated_duration_ms=0)
    return filter_and_validate(
        draft, set(CLAIM_TEXTS), WPM, grounding=GROUNDING, claim_texts=claim_texts
    )


def test_a_citation_of_the_right_claim_is_accepted() -> None:
    result = _validate(HAND_OFF, [BONDS])
    assert result.segments[0].claim_verification_ids == (BONDS,)


def test_known_limitation_a_citation_of_an_unrelated_claim_is_also_accepted() -> None:
    """The old behaviour, when no claim texts are supplied."""
    result = _validate(HAND_OFF, [REGULATORS, ADVISERS])
    assert result.segments[0].claim_verification_ids == (REGULATORS, ADVISERS)


def test_a_claim_id_is_enough_even_for_a_sentence_with_no_claim_in_it() -> None:
    result = _validate(
        "Gardens need sunlight and patience, just like any long project.", [ADVISERS]
    )
    assert result.segments[0].claim_verification_ids == (ADVISERS,)


def test_the_available_lexical_signal_is_too_weak_to_be_a_gate() -> None:
    """Why a bare "shares a content word" rule would not have caught the real case: the unrelated
    claims share words such as 'provide', 'investment' and 'goals' with the hand-off."""
    unrelated = {"text": " ".join([CLAIM_TEXTS[REGULATORS], CLAIM_TEXTS[ADVISERS]])}
    assert has_lexical_overlap(unrelated, HAND_OFF)


# --- with the claims' own wording ------------------------------------------------------------


def test_a_claim_that_shares_no_wording_with_the_segment_is_dropped() -> None:
    result = _validate(HAND_OFF, [REGULATORS, BONDS], claim_texts=CLAIM_TEXTS)
    assert result.segments[0].claim_verification_ids == (BONDS,)


def test_a_segment_left_with_no_relevant_claim_is_unsourced() -> None:
    text = "Gardens need sunlight and patience, just like any long project."
    with pytest.raises(UnsourcedSegmentError):
        _validate(text, [ADVISERS], claim_texts=CLAIM_TEXTS)


def test_a_connective_segment_survives_losing_all_its_citations() -> None:
    segment = ScriptSegmentDraft(
        segment_index=0,
        narration_text="Gardens need sunlight and patience.",
        claim_ids=[str(ADVISERS)],
        estimated_duration_ms=0,
        connective=True,
        topic_id=TOPIC,
    )
    draft = ScriptDraft(title="T", segments=[segment], estimated_duration_ms=0)
    result = filter_and_validate(draft, set(CLAIM_TEXTS), WPM, claim_texts=CLAIM_TEXTS)
    assert result.segments[0].claim_verification_ids == ()


def test_a_claim_whose_text_is_unknown_is_kept_not_judged() -> None:
    assert relevant_claim_ids(HAND_OFF, [REGULATORS], {}) == (REGULATORS,)


def test_known_limit_a_claim_on_the_same_subject_still_passes_but_is_flagged() -> None:
    """The real attempt-6 case: 'Investment Advisers provide ...' shares 'provide', 'goals' and
    'investment' with the fixed income hand-off, so the floor cannot drop it - the weak-coverage
    flag is what a reviewer sees."""
    kept = relevant_claim_ids(HAND_OFF, [ADVISERS], CLAIM_TEXTS)
    assert kept == (ADVISERS,)
    coverage = weak_citation_coverage(HAND_OFF, [ADVISERS], CLAIM_TEXTS)
    assert coverage is not None and coverage < WEAK_CITATION_COVERAGE


def test_the_right_claim_is_not_flagged() -> None:
    coverage = weak_citation_coverage(HAND_OFF, [BONDS], CLAIM_TEXTS)
    assert coverage is not None and coverage >= WEAK_CITATION_COVERAGE


def test_nothing_to_measure_is_none() -> None:
    assert weak_citation_coverage(HAND_OFF, [], CLAIM_TEXTS) is None
    assert weak_citation_coverage(HAND_OFF, [REGULATORS], {}) is None
