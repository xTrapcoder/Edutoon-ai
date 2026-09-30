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
  persisted script (the core promise). ``connective`` is the model's own claim about a
  segment, so it is not taken on trust: a segment longer than
  :data:`MAX_CONNECTIVE_WORDS` is too long to be "just a hand-off" and needs a citation like
  any other.
- no segment adds a fact the claims do not contain. With a :class:`GroundingReference`, every
  number, currency amount, month name and proper noun in a segment's narration - connective or
  not - must appear in the claims the writer was shown for that segment's topic, or in the
  project's and topics' own titles; otherwise :class:`UngroundedSegmentError`. This is the same
  token check scene text is held to (``storyboard_provenance``), and like it a safety guard and
  not fact verification: it never judges whether a sentence is *true*, only that it does not
  bring in a figure or a name from nowhere. It deliberately leaves explanatory wording alone -
  ordinary words, number words ("first", "one", "two"), everyday time words ("day", "year") and
  sentence-initial capitals are all free, because they are what teaching sounds like.
- claim ids are de-duplicated per segment, first-seen order preserved.
- each segment's ``estimated_duration_ms`` is re-derived here from its word
  count at ``words_per_minute`` - integer milliseconds (rule 5) - rather
  than trusting the value on the draft.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from edutoon.domain.script import ScriptDraft
from edutoon.domain.storyboard_provenance import unsupported_tokens

MAX_CONNECTIVE_WORDS: Final = 80
"""The longest a segment can be and still be accepted as purely connective (an introduction, a
hand-off, a short recap): about 30 seconds of narration, several sentences. A longer segment must
cite a supported claim."""

GROUNDED_KINDS: Final[frozenset[str]] = frozenset(
    {"number", "currency", "date_word", "proper_noun"}
)
"""What the grounding check protects: figures and names. Number words and unit words are not in
it - "first", "one", "day" and "year" are part of ordinary explanation, not facts."""


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


class UngroundedSegmentError(Exception):
    """A script segment brings in a number, date or name that none of its verified claims (nor
    the project's own titles) contain. Carries the offending ``segment_index`` and up to
    :data:`MAX_REPORTED_TOKENS` of the ``(kind, token)`` pairs found. The message is written for
    the project's owner."""

    def __init__(self, segment_index: int, tokens: Sequence[tuple[str, str]]) -> None:
        self.segment_index = segment_index
        self.tokens = tuple(tokens)
        shown = ", ".join(f"'{token}'" for _, token in self.tokens[:MAX_REPORTED_TOKENS])
        super().__init__(
            f"Script segment {segment_index + 1} mentions {shown}, which none of its verified "
            "claims contain, so it was rejected: nothing unsupported may reach the script. "
            "Generate the script again."
        )


MAX_REPORTED_TOKENS: Final = 5


@dataclass(frozen=True, slots=True)
class GroundingReference:
    """What a segment's wording may draw on besides the claims it cites.

    ``topic_texts`` maps a topic to its title plus the text of every claim the writer was shown
    for it (not only the ones a segment cites: a fact the topic's verified claims contain is
    supported wherever in the section it is used). ``frame_text`` is the project's title and every
    accepted topic's title - the lesson's own framing, which a hand-off may name.
    """

    topic_texts: Mapping[UUID | None, str]
    frame_text: str = ""

    def for_topic(self, topic_id: UUID | None) -> str:
        return f"{self.frame_text}\n{self.topic_texts.get(topic_id, '')}"


@dataclass(frozen=True, slots=True)
class ValidatedScriptSegment:
    segment_index: int
    narration_text: str
    connective: bool
    claim_verification_ids: tuple[UUID, ...]
    estimated_duration_ms: int
    topic_id: UUID | None = None


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
    *,
    grounding: GroundingReference | None = None,
) -> ValidatedScriptDraft:
    """Narrow ``draft``'s citations to ``supported_claim_ids`` and prove
    every fact-bearing segment keeps at least one. Raises
    :class:`UnsourcedSegmentError` otherwise; raises ``ValueError`` for a
    non-positive ``words_per_minute``.

    A segment marked ``connective`` is exempt from citing only while it is short
    (:data:`MAX_CONNECTIVE_WORDS`). With ``grounding``, every segment's figures and names are
    also checked against what its topic's claims contain (module docstring), raising
    :class:`UngroundedSegmentError`. Without it only the citation rules apply.
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
        word_count = len(segment.narration_text.split())
        connective = segment.connective and word_count <= MAX_CONNECTIVE_WORDS
        if not claim_verification_ids and not connective:
            raise UnsourcedSegmentError(index)
        if grounding is not None:
            unsupported = unsupported_tokens(
                segment.narration_text,
                grounding.for_topic(segment.topic_id),
                kinds=GROUNDED_KINDS,
            )
            if unsupported:
                raise UngroundedSegmentError(index, unsupported)

        validated.append(
            ValidatedScriptSegment(
                # Positions are assigned from order, never taken from the
                # draft (same discipline as the composition provider).
                segment_index=index,
                narration_text=segment.narration_text,
                connective=connective,
                claim_verification_ids=claim_verification_ids,
                estimated_duration_ms=_segment_duration_ms(
                    segment.narration_text, words_per_minute
                ),
                topic_id=segment.topic_id,
            )
        )

    total_duration_ms = sum(segment.estimated_duration_ms for segment in validated)
    return ValidatedScriptDraft(
        title=draft.title,
        segments=tuple(validated),
        estimated_duration_ms=total_duration_ms,
    )
