"""Script provenance rules (SG-3) - pure domain logic (rule 1: no I/O, no
imports of repositories / providers / services).

:func:`filter_and_validate` takes a raw :class:`~edutoon.domain.script.ScriptDraft`
(as produced by the composition provider) together with the set of claim
ids that actually earned a *supported* verdict from the Evidence Engine,
and returns an immutable :class:`ValidatedScriptDraft` in which:

- every surviving citation is a supported claim id. The provider already
  checked that cited ids were *supplied*; this narrows further to
  *supported* and silently drops anything else (including ids that are not
  even well-formed UUIDs).
- no fact-bearing segment is left uncited. A segment that ends up with no
  citations and was not explicitly marked ``connective`` raises
  :class:`UnsourcedSegmentError` - unsourced narration must never reach a
  persisted script (the core promise).
- claim ids are de-duplicated per segment, first-seen order preserved.
- each segment's ``estimated_duration_ms`` is re-derived here from its word
  count at ``words_per_minute`` - integer milliseconds (rule 5) - rather
  than trusting the value on the draft.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from edutoon.domain.script import ScriptDraft


class UnsourcedSegmentError(Exception):
    """A fact-bearing script segment has no supported citation.

    Raised by :func:`filter_and_validate` when, after unsupported claim ids
    are dropped, a segment has no citations left and was not marked
    ``connective``. Carries the offending ``segment_index``. A service that
    calls the domain is expected to translate this into a 422-class
    application error; the domain itself stays free of the web layer.
    """

    def __init__(self, segment_index: int) -> None:
        super().__init__(
            f"Script segment {segment_index} has no supported citation "
            "and is not marked connective."
        )
        self.segment_index = segment_index


@dataclass(frozen=True, slots=True)
class ValidatedScriptSegment:
    segment_index: int
    narration_text: str
    connective: bool
    claim_verification_ids: tuple[UUID, ...]
    estimated_duration_ms: int


@dataclass(frozen=True, slots=True)
class ValidatedScriptDraft:
    title: str
    segments: tuple[ValidatedScriptSegment, ...]
    estimated_duration_ms: int


def _coerce_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        return None


def _segment_duration_ms(narration_text: str, words_per_minute: int) -> int:
    """Words-per-minute estimate, in whole milliseconds (rule 5). An
    independent per-segment figure, never an accumulator (rule 6 concerns
    frame positions, derived elsewhere).
    """
    word_count = len(narration_text.split())
    return round(word_count / words_per_minute * 60_000)


def filter_and_validate(
    draft: ScriptDraft,
    supported_claim_ids: set[UUID],
    words_per_minute: int,
) -> ValidatedScriptDraft:
    """Narrow ``draft``'s citations to ``supported_claim_ids`` and prove
    every fact-bearing segment keeps at least one. Raises
    :class:`UnsourcedSegmentError` otherwise; raises ``ValueError`` for a
    non-positive ``words_per_minute``.
    """
    if words_per_minute <= 0:
        raise ValueError("words_per_minute must be positive.")

    validated: list[ValidatedScriptSegment] = []
    for index, segment in enumerate(draft.segments):
        kept: list[UUID] = []
        for raw_id in segment.claim_ids:
            claim_id = _coerce_uuid(raw_id)
            if claim_id is not None and claim_id in supported_claim_ids:
                kept.append(claim_id)

        claim_verification_ids = tuple(dict.fromkeys(kept))
        if not claim_verification_ids and not segment.connective:
            raise UnsourcedSegmentError(index)

        validated.append(
            ValidatedScriptSegment(
                # Positions are assigned from order, never taken from the
                # draft (same discipline as the composition provider).
                segment_index=index,
                narration_text=segment.narration_text,
                connective=segment.connective,
                claim_verification_ids=claim_verification_ids,
                estimated_duration_ms=_segment_duration_ms(
                    segment.narration_text, words_per_minute
                ),
            )
        )

    total_duration_ms = sum(segment.estimated_duration_ms for segment in validated)
    return ValidatedScriptDraft(
        title=draft.title,
        segments=tuple(validated),
        estimated_duration_ms=total_duration_ms,
    )
