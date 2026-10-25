"""Script-draft value objects shared by the composition provider (SG-2) and
the provenance rules (SG-3).

These live in the domain, not in ``providers``, so that pure domain code
(``script_provenance``) can name them without importing a provider
(rule 1). ``providers.llm`` imports them back the other way, which the layer
order permits.

A *draft* is the model's raw proposal: narration split into ordered
segments, each carrying the ids of the claims it says it drew on. Nothing
here is trusted yet - :func:`edutoon.domain.script_provenance.filter_and_validate`
is what turns a draft into something safe to persist.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ScriptSegmentDraft:
    """One proposed narration segment.

    ``claim_ids`` are the claim-id strings the composition provider handed
    the model (stringified claim-verification UUIDs); they are validated
    downstream, not here. ``connective`` marks a segment that asserts no
    fact of its own - an introduction or a hand-off - and so is allowed to
    carry no citations. ``topic_id`` (Phase 7) is never taken from the model:
    the service sets it to the accepted topic whose section this segment
    belongs to, since a section call only ever writes narration for one topic.
    """

    segment_index: int
    narration_text: str
    claim_ids: list[str]
    estimated_duration_ms: int
    connective: bool = False
    topic_id: UUID | None = None


MAX_MERGE_FACTOR = 4
"""A section may be merged down from at most this many times its segment limit. Beyond that the
reply is not a lesson written in short passages but something else, and is rejected."""


def merge_to_limit(
    segments: Sequence[ScriptSegmentDraft], limit: int, words_per_minute: int
) -> list[ScriptSegmentDraft]:
    """``segments`` joined, neighbour with neighbour, until there are at most ``limit``.

    A writer told to cover many claims may reply with many one-sentence passages (26 for a section
    whose limit is 12). That is not an error worth a failed generation, and splitting a lesson into
    a great many tiny passages would also make a scene for each. The two shortest neighbours are
    joined first, so passages stay about the same length, repeatedly. A joined passage cites every
    claim either had and is connective only if both were; its length is re-estimated.
    """
    merged = list(segments)
    while len(merged) > limit > 0:
        shortest = min(
            range(len(merged) - 1),
            key=lambda i: (
                len(merged[i].narration_text.split()) + len(merged[i + 1].narration_text.split())
            ),
        )
        first, second = merged[shortest], merged[shortest + 1]
        text = f"{first.narration_text.rstrip()} {second.narration_text.lstrip()}"
        merged[shortest : shortest + 2] = [
            replace(
                first,
                narration_text=text,
                claim_ids=list(dict.fromkeys([*first.claim_ids, *second.claim_ids])),
                connective=first.connective and second.connective,
                estimated_duration_ms=round(len(text.split()) / words_per_minute * 60_000),
            )
        ]
    return [replace(segment, segment_index=index) for index, segment in enumerate(merged)]


@dataclass(frozen=True, slots=True)
class ScriptDraft:
    title: str
    segments: list[ScriptSegmentDraft] = field(default_factory=list)
    estimated_duration_ms: int = 0


# --- Outline (Phase 7) -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TopicOutlineDraft:
    """One accepted topic's outline guidance from the model - informational
    only. Order and duration budget are never taken from an outline response
    (see ``domain.script_duration``): the server already fixed both before the
    outline call was made, from the topic's own reading-order position and its
    evidence-proportional budget.
    """

    topic_id: UUID
    purpose: str
    key_concepts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScriptOutlineDraft:
    title: str
    topics: tuple[TopicOutlineDraft, ...] = ()
