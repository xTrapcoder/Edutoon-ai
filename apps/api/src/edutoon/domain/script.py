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

from dataclasses import dataclass, field
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
