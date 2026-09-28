"""Claim-candidate vocabulary and validation - pure domain logic (rule 1: no
I/O, and no imports of repositories, providers or services).

Phase 6 turns each *accepted* topic into a handful of candidate factual
claims. A candidate is never a claim: it is only ever the seed text the
existing Evidence Engine (``services.evidence.verify_claim``,
``providers.llm.judge_claim`` - both unchanged by this module) independently
retrieves against and judges, exactly as it already does for a claim typed in
by hand. Nothing here marks anything verified, supported or true; the only
thing that survives past :func:`validate_candidates` is cleaned text.

:class:`ClaimCandidateLimits` is passed in explicitly, the same discipline as
:class:`edutoon.domain.topic.TopicLimits`: the service builds it from
settings, so the domain never reads configuration.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from edutoon.domain.topic_tree import normalise_term, term_key


@dataclass(frozen=True, slots=True)
class ClaimCandidateLimits:
    """Every bound :func:`validate_candidates` enforces.

    There are deliberately no defaults - see
    :class:`edutoon.domain.topic.TopicLimits` for why.
    """

    max_claims_per_topic: int
    max_claim_text_chars: int

    def __post_init__(self) -> None:
        for name in ("max_claims_per_topic", "max_claim_text_chars"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1.")


@dataclass(frozen=True, slots=True)
class ClaimCandidateDraft:
    """One candidate claim exactly as the model proposed it. Nothing in it
    is trusted yet.
    """

    text: str


@dataclass(frozen=True, slots=True)
class ClaimExtractionDraft:
    candidates: tuple[ClaimCandidateDraft, ...] = ()


def validate_candidates(
    draft: ClaimExtractionDraft,
    *,
    limits: ClaimCandidateLimits,
    existing_claim_texts: Collection[str] = (),
) -> list[str]:
    """Clean the model's raw proposal into candidate claim texts safe to hand
    to the Evidence Engine, preserving the model's own order.

    - Each candidate is normalised
      (:func:`edutoon.domain.topic_tree.normalise_term`); a blank one is
      dropped.
    - One over ``limits.max_claim_text_chars`` is dropped rather than cut: an
      atomic factual statement that needs truncating to fit was not a
      well-formed candidate in the first place.
    - Duplicates collapse to the first kept - of each other, and of
      ``existing_claim_texts`` (case- and whitespace-insensitive, via
      :func:`edutoon.domain.topic_tree.term_key`). ``existing_claim_texts`` is
      the topic's own already-extracted claim texts, so a replace/re-run never
      re-verifies (and re-records) a claim this topic already has - it is
      genuinely incremental, not a wholesale redo.
    - The result is capped at ``limits.max_claims_per_topic``.

    Returns an empty list, never raises, when nothing survives: a topic
    genuinely having nothing new to state - most often every proposal being a
    duplicate of one already extracted - is an ordinary outcome, not a
    failure.
    """
    seen = {term_key(text) for text in existing_claim_texts}
    kept: list[str] = []
    for candidate in draft.candidates:
        text = normalise_term(candidate.text)
        if not text or len(text) > limits.max_claim_text_chars:
            continue
        key = term_key(text)
        if key in seen:
            continue
        seen.add(key)
        kept.append(text)
        if len(kept) == limits.max_claims_per_topic:
            break
    return kept
