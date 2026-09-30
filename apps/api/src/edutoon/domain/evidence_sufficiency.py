"""Is there enough verified evidence for the video the user asked for? - pure domain logic (rule 1:
no I/O, no imports of repositories / providers / services, and no configuration reads).

A script may state only what a *supported* claim supports (the core promise). The duration
allocator (:mod:`edutoon.domain.script_duration`) will happily share any target across any number
of topics, so a five-minute target over three one-sentence claims still asks the model for five
minutes of narration - and the only way to fill it is to repeat the same fact or invent new ones.
This module refuses that *before any paid call is made*, from counts alone.

**The heuristic, and why it is a heuristic.** How long a claim can honestly be talked about is not
a fact, so nothing here pretends to measure it. The rule is deliberately conservative and simple:

* a supported claim can be explained - its terms defined, its reasoning walked through, an
  example given that states no new fact - for at most :data:`DEFAULT_MS_PER_CLAIM` (30 seconds,
  about 75 words at the narration rate: several times the claim's own length);
* each topic also earns :data:`CONNECTIVE_MS_PER_TOPIC` of connective narration (introducing it,
  handing off to the next), and the lesson :data:`LESSON_FRAME_MS` for an opening and a close;
* a topic can use at most ``max_claims_per_topic`` claims (all the script writer is shown for one
  topic, ``MAX_CLAIMS_PER_SECTION``), so claims beyond that add nothing.

The *defensible duration* is the sum; a target above it is refused. It errs towards refusing,
because a refused request costs the user a click and a padded script costs them their trust in
every claim on the screen; but it is not tuned to refuse healthy lessons - a five-minute lesson
needs about a dozen supported claims across a handful of topics, which is what a few pages of
source material yield. The per-claim allowance is configuration (``EVIDENCE_SECONDS_PER_CLAIM``) so
it can be re-tuned against the first real-provider evaluation; nothing else about it is a knob.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

DEFAULT_MS_PER_CLAIM: Final = 30_000
"""The most narration one supported claim is allowed to carry."""

CONNECTIVE_MS_PER_TOPIC: Final = 10_000
"""Narration a topic earns for introducing itself and handing off, asserting no fact."""

LESSON_FRAME_MS: Final = 15_000
"""Narration the lesson earns for an opening and a conclusion, asserting no fact."""


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    """How the evidence compares with the target."""

    target_duration_ms: int
    defensible_duration_ms: int
    topic_count: int
    supported_claims: int
    usable_claims: int
    """Claims that count: each topic's supported claims up to ``max_claims_per_topic``."""

    @property
    def is_sufficient(self) -> bool:
        return self.target_duration_ms <= self.defensible_duration_ms

    @property
    def shortfall_ms(self) -> int:
        return max(0, self.target_duration_ms - self.defensible_duration_ms)


def defensible_duration_ms(
    claim_counts: Sequence[int],
    *,
    max_claims_per_topic: int,
    ms_per_claim: int = DEFAULT_MS_PER_CLAIM,
) -> int:
    """How much narration ``claim_counts`` (one supported-claim count per topic) can honestly
    fill: per topic, its usable claims times ``ms_per_claim`` plus its connective allowance;
    plus the lesson's frame. Zero for no topics at all. Raises :class:`ValueError` for a
    negative count or a non-positive limit or allowance."""
    if max_claims_per_topic < 1 or ms_per_claim < 1:
        raise ValueError("max_claims_per_topic and ms_per_claim must be positive.")
    if any(count < 0 for count in claim_counts):
        raise ValueError("A claim count must not be negative.")
    if not claim_counts:
        return 0
    per_topic = sum(
        min(count, max_claims_per_topic) * ms_per_claim + CONNECTIVE_MS_PER_TOPIC
        for count in claim_counts
    )
    return per_topic + LESSON_FRAME_MS


def assess_evidence(
    claim_counts: Sequence[int],
    *,
    target_duration_ms: int,
    max_claims_per_topic: int,
    ms_per_claim: int = DEFAULT_MS_PER_CLAIM,
) -> EvidenceAssessment:
    """Compare a project's evidence (one supported-claim count per accepted topic) with its
    target. Pure and deterministic: the same counts and target always give the same answer."""
    return EvidenceAssessment(
        target_duration_ms=target_duration_ms,
        defensible_duration_ms=defensible_duration_ms(
            claim_counts, max_claims_per_topic=max_claims_per_topic, ms_per_claim=ms_per_claim
        ),
        topic_count=len(claim_counts),
        supported_claims=sum(claim_counts),
        usable_claims=sum(min(count, max_claims_per_topic) for count in claim_counts),
    )


def format_clock(duration_ms: int) -> str:
    """``m:ss`` (or ``h:mm:ss`` from an hour), rounded down to whole seconds - how a duration is
    written in a message: ``225_000`` is ``3:45``."""
    total_seconds = max(0, duration_ms) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
