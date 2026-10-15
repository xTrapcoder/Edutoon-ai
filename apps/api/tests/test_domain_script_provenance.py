"""``domain/script_provenance.py`` - pure, no fixtures, no DB.

``filter_and_validate`` turns a raw script draft into a validated one:
citations narrowed to the supported set, every fact-bearing segment proven
to keep at least one, durations recomputed from word count.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from edutoon.domain.script import ScriptDraft, ScriptSegmentDraft
from edutoon.domain.script_provenance import (
    UnsourcedSegmentError,
    ValidatedScriptDraft,
    filter_and_validate,
)

WPM = 150


def _segment(
    narration: str,
    claim_ids: list[str],
    *,
    index: int = 0,
    connective: bool = False,
) -> ScriptSegmentDraft:
    return ScriptSegmentDraft(
        segment_index=index,
        narration_text=narration,
        claim_ids=claim_ids,
        estimated_duration_ms=0,  # deliberately wrong - the domain recomputes
        connective=connective,
    )


def _draft(*segments: ScriptSegmentDraft, title: str = "A script") -> ScriptDraft:
    return ScriptDraft(title=title, segments=list(segments), estimated_duration_ms=0)


def test_validates_a_well_sourced_draft() -> None:
    c1, c2 = uuid4(), uuid4()
    draft = _draft(
        _segment("Plants make food from light.", [str(c1)], index=0),
        _segment("Chlorophyll is the pigment involved.", [str(c2)], index=1),
    )

    result = filter_and_validate(draft, {c1, c2}, WPM)

    assert isinstance(result, ValidatedScriptDraft)
    assert result.title == "A script"
    assert [s.segment_index for s in result.segments] == [0, 1]
    assert result.segments[0].claim_verification_ids == (c1,)
    assert result.segments[1].claim_verification_ids == (c2,)


def test_unsourced_fact_bearing_segment_is_rejected() -> None:
    c1 = uuid4()
    draft = _draft(
        _segment("A well sourced sentence.", [str(c1)], index=0),
        _segment("An unsourced assertion.", [], index=1),
    )

    with pytest.raises(UnsourcedSegmentError) as excinfo:
        filter_and_validate(draft, {c1}, WPM)

    assert excinfo.value.segment_index == 1


def test_segment_left_unsourced_after_filtering_is_rejected() -> None:
    supported, dropped = uuid4(), uuid4()
    draft = _draft(_segment("Rests only on a dropped claim.", [str(dropped)]))

    with pytest.raises(UnsourcedSegmentError):
        filter_and_validate(draft, {supported}, WPM)


def test_connective_segment_may_carry_no_citations() -> None:
    c1 = uuid4()
    draft = _draft(
        _segment("Let us look at how plants feed.", [], index=0, connective=True),
        _segment("They turn light into chemical energy.", [str(c1)], index=1),
    )

    result = filter_and_validate(draft, {c1}, WPM)

    assert result.segments[0].connective is True
    assert result.segments[0].claim_verification_ids == ()


def test_claim_ids_outside_the_supported_set_are_dropped() -> None:
    supported, unsupported = uuid4(), uuid4()
    draft = _draft(_segment("Cites one good, one bad.", [str(supported), str(unsupported)]))

    result = filter_and_validate(draft, {supported}, WPM)

    assert result.segments[0].claim_verification_ids == (supported,)


def test_malformed_claim_id_is_dropped_not_raised() -> None:
    supported = uuid4()
    draft = _draft(_segment("Has a junk id too.", [str(supported), "not-a-uuid"]))

    result = filter_and_validate(draft, {supported}, WPM)

    assert result.segments[0].claim_verification_ids == (supported,)


def test_duplicate_claim_ids_are_deduplicated_in_order() -> None:
    c1, c2 = uuid4(), uuid4()
    draft = _draft(_segment("Repeats a citation.", [str(c1), str(c2), str(c1), str(c2)]))

    result = filter_and_validate(draft, {c1, c2}, WPM)

    assert result.segments[0].claim_verification_ids == (c1, c2)


def test_duration_is_computed_from_word_count() -> None:
    c1 = uuid4()
    narration = " ".join(["word"] * 90)  # 90 words
    draft = _draft(_segment(narration, [str(c1)]))

    result = filter_and_validate(draft, {c1}, 180)

    # 90 words / 180 wpm = 0.5 min = 30_000 ms.
    assert result.segments[0].estimated_duration_ms == 30_000
    assert result.estimated_duration_ms == 30_000


def test_total_duration_is_the_sum_of_segment_estimates() -> None:
    c1, c2 = uuid4(), uuid4()
    draft = _draft(
        _segment(" ".join(["w"] * 60), [str(c1)], index=0),
        _segment(" ".join(["w"] * 30), [str(c2)], index=1),
    )

    result = filter_and_validate(draft, {c1, c2}, 60)

    assert [s.estimated_duration_ms for s in result.segments] == [60_000, 30_000]
    assert result.estimated_duration_ms == 90_000


def test_segment_positions_are_assigned_from_order_not_the_draft() -> None:
    c1, c2 = uuid4(), uuid4()
    draft = _draft(
        _segment("First.", [str(c1)], index=7),
        _segment("Second.", [str(c2)], index=42),
    )

    result = filter_and_validate(draft, {c1, c2}, WPM)

    assert [s.segment_index for s in result.segments] == [0, 1]


def test_non_positive_words_per_minute_is_a_value_error() -> None:
    c1 = uuid4()
    draft = _draft(_segment("Anything.", [str(c1)]))

    with pytest.raises(ValueError):
        filter_and_validate(draft, {c1}, 0)


def test_validated_structure_is_immutable() -> None:
    c1 = uuid4()
    draft = _draft(_segment("Immutable please.", [str(c1)]))

    result = filter_and_validate(draft, {c1}, WPM)

    assert isinstance(result.segments, tuple)
    assert isinstance(result.segments[0].claim_verification_ids, tuple)
    with pytest.raises(AttributeError):
        result.title = "changed"  # type: ignore[misc]


def test_empty_draft_validates_to_an_empty_result() -> None:
    result = filter_and_validate(_draft(), set[UUID](), WPM)

    assert result.segments == ()
    assert result.estimated_duration_ms == 0
